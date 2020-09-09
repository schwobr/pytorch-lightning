# Copyright The PyTorch Lightning team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import subprocess
import numpy as np
import torch
import torch.distributed as torch_distrib
from pytorch_lightning.utilities.model_utils import is_overridden
from pytorch_lightning.trainer.supporters import TensorRunningAccum, Accumulator
from pytorch_lightning.callbacks import ModelCheckpoint
from pytorch_lightning import _logger as log
from pytorch_lightning.utilities.memory import recursive_detach
from pytorch_lightning.utilities.exceptions import MisconfigurationException
from pytorch_lightning.core.step_result import EvalResult, Result
from pytorch_lightning.utilities.parsing import AttributeDict
from copy import copy, deepcopy


class TrainLoop:

    def __init__(self, trainer):
        self.trainer = trainer
        self.should_check_val = False
        self.early_stopping_accumulator = None
        self.checkpoint_accumulator = None
        self.accumulated_loss = None
        self._teardown_already_run = False

    @property
    def num_optimizers(self):
        num_optimizers = len(self.get_optimizers_iterable())
        return num_optimizers

    def on_train_start(self):
        # clear cache before training
        if self.trainer.on_gpu and self.trainer.root_gpu is not None:
            # use context because of:
            # https://discuss.pytorch.org/t/out-of-memory-when-i-use-torch-cuda-empty-cache/57898
            with torch.cuda.device(f'cuda:{self.trainer.root_gpu}'):
                torch.cuda.empty_cache()

        # hook
        self.trainer.call_hook('on_train_start')

    def on_train_end(self):
        if self._teardown_already_run:
            return

        self._teardown_already_run = True

        # Save latest checkpoint
        log.info('Saving latest checkpoint..')
        self.check_checkpoint_callback(should_check_val=False)

        # hook
        self.trainer.call_hook('on_train_end')

        # kill loggers
        if self.trainer.logger is not None:
            self.trainer.logger.finalize("success")

        # summarize profile results
        if self.trainer.global_rank == 0:
            self.trainer.profiler.describe()

        if self.trainer.global_rank == 0:
            for proc in self.trainer.interactive_ddp_procs:
                subprocess.Popen.kill(proc)

        # clean up dist group
        if self.trainer.use_ddp or self.trainer.use_ddp2:
            torch_distrib.destroy_process_group()

        # clear mem
        if self.trainer.on_gpu:
            model = self.trainer.get_model()
            model.cpu()
            torch.cuda.empty_cache()

    def check_checkpoint_callback(self, should_check_val):
        model = self.trainer.get_model()

        # when no val loop is present or fast-dev-run still need to call checkpoints
        # TODO bake this logic into the checkpoint callback
        should_activate = not is_overridden('validation_step', model) and not should_check_val
        if should_activate:
            checkpoint_callbacks = [c for c in self.trainer.callbacks if isinstance(c, ModelCheckpoint)]
            [c.on_validation_end(self.trainer, model) for c in checkpoint_callbacks]

    def on_train_epoch_start(self, epoch):
        model = self.trainer.get_model()

        # set seed for distributed sampler (enables shuffling for each epoch)
        try:
            self.trainer.train_dataloader.sampler.set_epoch(epoch)
        except Exception:
            pass

        # update training progress in trainer and model
        model.current_epoch = epoch
        self.trainer.current_epoch = epoch

        # changing gradient according accumulation_scheduler
        self.trainer.accumulation_scheduler.on_epoch_start(self.trainer, self.trainer.get_model())

        # stores accumulated grad fractions per batch
        self.accumulated_loss = TensorRunningAccum(
            window_length=self.trainer.accumulate_grad_batches
        )

        # bookkeeping
        self.should_check_val = False

        # structured result accumulators for callbacks
        self.early_stopping_accumulator = Accumulator()
        self.checkpoint_accumulator = Accumulator()

        # hook
        self.trainer.call_hook('on_epoch_start')
        self.trainer.call_hook('on_train_epoch_start')

    def on_train_batch_end(self, epoch_output, epoch_end_outputs, batch, batch_idx, dataloader_idx):
        # figure out what to track for epoch end
        self.track_epoch_end_reduce_metrics(epoch_output, epoch_end_outputs)

        # hook
        self.trainer.call_hook('on_batch_end')
        self.trainer.call_hook('on_train_batch_end', batch, batch_idx, dataloader_idx)

    def reset_train_val_dataloaders(self, model):
        if not self.trainer.reload_dataloaders_every_epoch:
            self.trainer.reset_train_dataloader(model)

        if self.trainer.val_dataloaders is None and not self.trainer.reload_dataloaders_every_epoch:
            self.trainer.reset_val_dataloader(model)

    def track_epoch_end_reduce_metrics(self, epoch_output, epoch_end_outputs):
        # track the outputs to reduce at the end of the epoch
        for opt_idx, opt_outputs in enumerate(epoch_end_outputs):
            # with 1 step (no tbptt) don't use a sequence at epoch end
            if isinstance(opt_outputs, list) and len(opt_outputs) == 1 and not isinstance(opt_outputs[0], Result):
                opt_outputs = opt_outputs[0]
            epoch_output[opt_idx].append(opt_outputs)

    def get_optimizers_iterable(self):
        """
        Generates an iterable with (idx, optimizer) for each optimizer.
        """
        if not self.trainer.optimizer_frequencies:
            # call training_step once per optimizer
            return list(enumerate(self.trainer.optimizers))

        optimizer_freq_cumsum = np.cumsum(self.trainer.optimizer_frequencies)
        optimizers_loop_length = optimizer_freq_cumsum[-1]
        current_place_in_loop = self.trainer.total_batch_idx % optimizers_loop_length

        # find optimzier index by looking for the first {item > current_place} in the cumsum list
        opt_idx = np.argmax(optimizer_freq_cumsum > current_place_in_loop)
        return [(opt_idx, self.trainer.optimizers[opt_idx])]

    def backward(self, result, optimizer, opt_idx):
        # backward pass
        with self.trainer.profiler.profile('model_backward'):
            result.closure_loss = self.trainer.accelerator_backend.backward(result.closure_loss, optimizer, opt_idx)

    def on_after_backward(self, training_step_output, batch_idx, untouched_loss):
        is_result_obj = isinstance(training_step_output, Result)

        if is_result_obj:
            training_step_output.detach()
        else:
            training_step_output.batch_loss = training_step_output.batch_loss.detach()

        # insert after step hook
        self.trainer.call_hook('on_after_backward')

        # when in dev debugging track the losses
        self.trainer.dev_debugger.track_train_loss_history(batch_idx, untouched_loss.detach())

    def training_step(self, split_batch, batch_idx, opt_idx, hiddens):
        with self.trainer.profiler.profile('model_forward'):
            args = self.build_train_args(split_batch, batch_idx, opt_idx, hiddens)
            training_step_output = self.trainer.accelerator_backend.training_step(args)
            training_step_output = self.trainer.call_hook('training_step_end', training_step_output)

            # ----------------------------
            # PROCESS THE RESULT
            # ----------------------------
            # format and reduce outputs accordingly
            training_step_output_for_epoch_end = training_step_output
            is_result_obj = isinstance(training_step_output, Result)

            # track batch size for weighted average
            if is_result_obj:
                training_step_output.track_batch_size(len(split_batch))

            # don't allow EvalResult in the training_step
            if isinstance(training_step_output, EvalResult):
                raise MisconfigurationException('training_step cannot return EvalResult, '
                                                'use a dict or TrainResult instead')

            # handle regular dicts
            if not is_result_obj:
                training_step_output = self.trainer.process_output(training_step_output, train=True)

                training_step_output = AttributeDict(
                    batch_loss=training_step_output[0],
                    pbar_on_batch_end=training_step_output[1],
                    log_metrics=training_step_output[2],
                    callback_metrics=training_step_output[3],
                    hiddens=training_step_output[4],
                )

            # if the user decides to finally reduce things in epoch_end, save raw output without graphs
            if isinstance(training_step_output_for_epoch_end, torch.Tensor):
                training_step_output_for_epoch_end = training_step_output_for_epoch_end.detach()
            elif is_result_obj:
                training_step_output_for_epoch_end = copy(training_step_output)
                training_step_output_for_epoch_end.detach()
            else:
                training_step_output_for_epoch_end = recursive_detach(training_step_output_for_epoch_end)

        # accumulate loss
        # (if accumulate_grad_batches = 1 no effect)
        closure_loss = training_step_output.minimize if is_result_obj else training_step_output.batch_loss
        closure_loss = closure_loss / self.trainer.accumulate_grad_batches

        # the loss will get scaled for amp. avoid any modifications to it
        untouched_loss = closure_loss.detach().clone()

        # result
        result = AttributeDict(
            closure_loss=closure_loss,
            loss=untouched_loss,
            training_step_output=training_step_output,
            training_step_output_for_epoch_end=training_step_output_for_epoch_end,
            hiddens=training_step_output.hiddens,
        )
        return result

    def optimizer_step(self, optimizer, opt_idx, batch_idx, train_step_and_backward_closure):
        with self.trainer.profiler.profile('optimizer_step'):
            # optimizer step lightningModule hook
            self.trainer.accelerator_backend.optimizer_step(optimizer, batch_idx, opt_idx,
                                                            train_step_and_backward_closure)

    def on_before_zero_grad(self, optimizer):
        model = self.trainer.get_model()
        model.on_before_zero_grad(optimizer)

    def optimizer_zero_grad(self, batch_idx, optimizer, opt_idx):
        self.trainer.accelerator_backend.optimizer_zero_grad(batch_idx, optimizer, opt_idx)

    def on_before_backward(self, batch_idx, optimizer):
        # track gradient norms
        grad_norm_dic = self._track_gradient_norm(batch_idx)

        # clip gradients
        self.trainer.accelerator_backend.clip_gradients(optimizer)
        return grad_norm_dic

    def _track_gradient_norm(self, batch_idx):
        grad_norm_dic = {}
        if batch_idx % self.trainer.row_log_interval == 0:
            if float(self.trainer.track_grad_norm) > 0:
                model = self.trainer.get_model()
                grad_norm_dic = model.grad_norm(
                    self.trainer.track_grad_norm)
        return grad_norm_dic

    def log_training_step_metrics(self, opt_closure_result, batch_callback_metrics, batch_log_metrics):
        # track callback metrics
        callback_metrics = opt_closure_result.training_step_output.callback_metrics
        batch_callback_metrics.append(callback_metrics)

        # decide which metrics to log (results vs dict return)
        using_results_obj = isinstance(opt_closure_result.training_step_output, Result)
        if using_results_obj:
            metrics_to_log = opt_closure_result.training_step_output.batch_log_metrics
            step_pbar_metrics = opt_closure_result.training_step_output.batch_pbar_metrics
        else:
            metrics_to_log = opt_closure_result.training_step_output.log_metrics
            step_pbar_metrics = opt_closure_result.training_step_output.pbar_on_batch_end

        # track batch log metrics
        batch_log_metrics.append(metrics_to_log)

        # track progress bar metrics
        if len(step_pbar_metrics) > 0:
            self.trainer.logger_connector.add_progress_bar_metrics(step_pbar_metrics)

    def process_hiddens(self, opt_closure_result):
        hiddens = opt_closure_result.hiddens
        if isinstance(opt_closure_result.training_step_output, Result):
            opt_closure_result.training_step_output_for_epoch_end.drop_hiddens()
        return hiddens

    def tbptt_split_batch(self, batch):
        splits = [batch]
        if self.trainer.truncated_bptt_steps is not None:
            model_ref = self.trainer.get_model()
            with self.trainer.profiler.profile('tbptt_split_batch'):
                splits = model_ref.tbptt_split_batch(batch, self.trainer.truncated_bptt_steps)
        return splits

    def run_training_epoch(self):

        # get model
        model = self.trainer.get_model()

        # modify dataloader if needed (ddp, etc...)
        train_dataloader = self.trainer.accelerator_backend.process_dataloader(self.trainer.train_dataloader)

        # track epoch output
        epoch_output = [[] for _ in range(self.num_optimizers)]

        # enable profiling for the dataloader
        train_dataloader = self.trainer.data_connector.get_profiled_train_dataloader(train_dataloader)
        dataloader_idx = 0
        for batch_idx, (batch, is_last_batch) in train_dataloader:
            # stop epoch if we limited the number of training batches
            if batch_idx >= self.trainer.num_training_batches:
                break

            self.trainer.batch_idx = batch_idx
            model.global_step = self.trainer.global_step

            # ------------------------------------
            # TRAINING_STEP + TRAINING_STEP_END
            # ------------------------------------
            batch_output = self.run_training_batch(batch, batch_idx, dataloader_idx)

            # only track outputs when user implements training_epoch_end
            # otherwise we will build up unnecessary memory
            epoch_end_outputs = self.process_train_step_outputs(
                batch_output.training_step_output_for_epoch_end,
                self.early_stopping_accumulator,
                self.checkpoint_accumulator
            )

            # hook
            self.on_train_batch_end(epoch_output, epoch_end_outputs, batch, batch_idx, dataloader_idx)

            # when returning -1 from train_step, we end epoch early
            self.trainer.should_stop = batch_output.signal == -1

            # -----------------------------------------
            # VALIDATE IF NEEDED + CHECKPOINT CALLBACK
            # -----------------------------------------
            should_check_val = self.should_check_val_fx(batch_idx, is_last_batch)
            if should_check_val:
                self.trainer.run_evaluation(test_mode=False)

            # -----------------------------------------
            # SAVE LOGGERS (ie: Tensorboard, etc...)
            # -----------------------------------------
            self.save_loggers_on_train_batch_end(batch_idx)

            # -----------------------------------------
            # SAVE METRICS TO LOGGERS
            # -----------------------------------------
            self.trainer.logger_connector.save_train_loop_metrics_to_loggers(batch_idx, batch_output)

            # update LR schedulers
            monitor_metrics = deepcopy(self.trainer.logger_connector.callback_metrics)
            monitor_metrics.update(batch_output.batch_log_metrics)
            self.update_train_loop_lr_schedulers(monitor_metrics=monitor_metrics)

            # progress global step according to grads progress
            self.increment_accumulated_grad_global_step()

            # max steps reached, end training
            if self.trainer.max_steps is not None and self.trainer.max_steps == self.trainer.global_step:
                break

            # end epoch early
            # stop when the flag is changed or we've gone past the amount
            # requested in the batches
            if self.trainer.should_stop:
                break

        # process epoch outputs
        self.trainer.logger_connector.on_train_epoch_end(
            epoch_output,
            self.checkpoint_accumulator,
            self.early_stopping_accumulator,
            self.num_optimizers
        )

        # checkpoint callback
        self.check_checkpoint_callback(self.should_check_val)

        # epoch end hook
        self.run_on_epoch_end_hook()

    def run_training_batch(self, batch, batch_idx, dataloader_idx):
        # track grad norms
        grad_norm_dic = {}

        # track all metrics for callbacks
        batch_callback_metrics = []

        # track metrics to log
        batch_log_metrics = []

        # bookkeeping
        using_results_obj = False
        self.trainer.hiddens = None

        # track all outputs across time and num of optimizers
        batch_outputs = [[] for _ in range(len(self.get_optimizers_iterable()))]

        if batch is None:
            return AttributeDict(signal=0, grad_norm_dic=grad_norm_dic)

        # hook
        response = self.trainer.call_hook('on_batch_start')
        if response == -1:
            return AttributeDict(signal=-1, grad_norm_dic=grad_norm_dic)

        # hook
        response = self.trainer.call_hook('on_train_batch_start', batch, batch_idx, dataloader_idx)
        if response == -1:
            return AttributeDict(signal=-1, grad_norm_dic=grad_norm_dic)

        # lightning module hook
        splits = self.tbptt_split_batch(batch)

        for split_idx, split_batch in enumerate(splits):
            self.trainer.split_idx = split_idx

            # loop over optimizers
            for opt_idx, optimizer in self.get_optimizers_iterable():
                # make sure only the gradients of the current optimizer's parameters are calculated
                # in the training step to prevent dangling gradients in multiple-optimizer setup.
                if len(self.trainer.optimizers) > 1:
                    for param in self.trainer.get_model().parameters():
                        param.requires_grad = False
                    for group in optimizer.param_groups:
                        for param in group['params']:
                            param.requires_grad = True

                # -------------------
                # calculate loss (train step + train step end)
                # -------------------
                opt_closure_result = self.training_step_and_backward(
                    split_batch,
                    batch_idx,
                    opt_idx,
                    optimizer,
                    self.trainer.hiddens
                )

                # log metrics
                self.log_training_step_metrics(opt_closure_result, batch_callback_metrics, batch_log_metrics)

                # track hiddens
                self.trainer.hiddens = self.process_hiddens(opt_closure_result)

                # check if loss or model weights are nan
                if self.trainer.terminate_on_nan:
                    self.trainer.detect_nan_tensors(opt_closure_result.loss)

                # track total loss for logging (avoid mem leaks)
                self.accumulated_loss.append(opt_closure_result.loss)

                # track all the outputs across all steps
                batch_outputs[opt_idx].append(opt_closure_result.training_step_output_for_epoch_end)

                # ------------------------------
                # BACKWARD PASS
                # ------------------------------
                # gradient update with accumulated gradients
                accumulation_done = (self.trainer.batch_idx + 1) % self.trainer.accumulate_grad_batches == 0
                is_final_batch = (self.trainer.batch_idx + 1) == self.trainer.num_training_batches
                if accumulation_done or is_final_batch:
                    # hook
                    grad_norm_dic = self.on_before_backward(batch_idx, optimizer)

                    # wrap forward + backward pass in closure for 2nd order optimizers
                    train_step_and_backward_closure = lambda: self.training_step_and_backward(
                        split_batch, batch_idx, opt_idx, optimizer, self.trainer.hiddens,
                    ).loss

                    # optimizer step
                    self.optimizer_step(optimizer, opt_idx, batch_idx, train_step_and_backward_closure)

                    # hook
                    self.on_before_zero_grad(optimizer)

                    # clear gradients
                    self.optimizer_zero_grad(batch_idx, optimizer, opt_idx)

                    # calculate running loss for display
                    self.trainer.running_loss.append(
                        self.accumulated_loss.mean() * self.trainer.accumulate_grad_batches
                    )

                    # reset for next set of accumulated grads
                    self.accumulated_loss.reset()

        # collapse all metrics into one dict
        batch_log_metrics = {k: v for d in batch_log_metrics for k, v in d.items()}

        # track all metrics for callbacks
        if not using_results_obj:
            self.trainer.logger_connector.callback_metrics.update(
                {k: v for d in batch_callback_metrics for k, v in d.items()}
            )

        result = AttributeDict(
            signal=0,
            grad_norm_dic=grad_norm_dic,
            batch_log_metrics=batch_log_metrics,
            training_step_output_for_epoch_end=batch_outputs
        )
        return result

    def training_step_and_backward(self, split_batch, batch_idx, opt_idx, optimizer, hiddens):
        """
        wrap the forward step in a closure so second order methods work
        """
        # lightning module hook
        result = self.training_step(split_batch, batch_idx, opt_idx, hiddens)

        # backward pass
        self.backward(result, optimizer, opt_idx)

        # hook
        self.on_after_backward(result.training_step_output, batch_idx, result.loss)

        return result

    def update_train_loop_lr_schedulers(self, monitor_metrics=None):
        num_accumulated_batches_reached = (self.trainer.batch_idx + 1) % self.trainer.accumulate_grad_batches == 0
        num_training_batches_reached = (self.trainer.batch_idx + 1) == self.trainer.num_training_batches

        if num_accumulated_batches_reached or num_training_batches_reached:
            # update lr
            self.trainer.lr_scheduler_connector.update_learning_rates(interval='step', monitor_metrics=monitor_metrics)

    def run_on_epoch_end_hook(self):
        self.trainer.call_hook('on_epoch_end')
        self.trainer.call_hook('on_train_epoch_end')

    def increment_accumulated_grad_global_step(self):
        num_accumulated_batches_reached = (self.trainer.batch_idx + 1) % self.trainer.accumulate_grad_batches == 0
        num_training_batches_reached = (self.trainer.batch_idx + 1) == self.trainer.num_training_batches

        # progress global step according to grads progress
        if num_accumulated_batches_reached or num_training_batches_reached:
            self.trainer.global_step += 1
        self.trainer.total_batch_idx += 1

    def should_check_val_fx(self, batch_idx, is_last_batch):
        # decide if we should run validation
        is_val_check_batch = (batch_idx + 1) % self.trainer.val_check_batch == 0
        can_check_epoch = (self.trainer.current_epoch + 1) % self.trainer.check_val_every_n_epoch == 0
        can_check_val = self.trainer.enable_validation and can_check_epoch
        should_check_val = is_val_check_batch or self.trainer.should_stop
        is_last_batch_for_infinite_dataset = (is_last_batch and self.trainer.val_check_batch == float('inf'))
        should_check_val = can_check_val and (should_check_val or is_last_batch_for_infinite_dataset)

        return should_check_val

    def build_train_args(self, batch, batch_idx, opt_idx, hiddens):
        # enable not needing to add opt_idx to training_step
        args = [batch, batch_idx]

        if len(self.trainer.optimizers) > 1:
            if self.trainer.has_arg('training_step', 'optimizer_idx'):
                args.append(opt_idx)
            else:
                num_opts = len(self.trainer.optimizers)
                raise ValueError(
                    f'Your LightningModule defines {num_opts} optimizers but '
                    f'training_step is missing the "optimizer_idx" argument.'
                )

        # pass hiddens if using tbptt
        if self.trainer.truncated_bptt_steps is not None:
            args.append(hiddens)

        return args

    def save_loggers_on_train_batch_end(self, batch_idx):
        # when loggers should save to disk
        should_save_log = (batch_idx + 1) % self.trainer.log_save_interval == 0 or self.trainer.should_stop
        if should_save_log or self.trainer.fast_dev_run:
            if self.trainer.is_global_zero and self.trainer.logger is not None:
                self.trainer.logger.save()

    def process_train_step_outputs(self, all_train_step_outputs, early_stopping_accumulator, checkpoint_accumulator):
        """
        Figure out what needs to be tracked/logged at the end of the epoch
        """

        # the training step outputs a list per optimizer. The list contains the outputs at each time step
        # when no TBPTT is used, then the list has 1 item per batch
        # when TBPTT IS used, then the list has n items (1 per time step)
        epoch_end_outputs = []
        for optimizer_idx_outputs in all_train_step_outputs:
            # extract one representative sample from each time step (1 if no tbptt) and 0th optimizer
            sample_output = optimizer_idx_outputs[-1]

            # pull out callback info if available (ie: Results object)
            if isinstance(sample_output, dict) and 'early_stop_on' in sample_output:
                early_stopping_accumulator.accumulate(sample_output['early_stop_on'])

            if isinstance(sample_output, dict) and 'checkpoint_on' in sample_output:
                checkpoint_accumulator.accumulate(sample_output['checkpoint_on'])

            # decide if we need to reduce at the end of the epoch automatically
            auto_reduce_tng_result = isinstance(sample_output, Result) and sample_output.should_reduce_on_epoch_end

            # only track when a) it needs to be autoreduced OR b) the user wants to manually reduce on epoch end
            if is_overridden('training_epoch_end', model=self.trainer.get_model()) or auto_reduce_tng_result:
                epoch_end_outputs.append(optimizer_idx_outputs)

        return epoch_end_outputs
