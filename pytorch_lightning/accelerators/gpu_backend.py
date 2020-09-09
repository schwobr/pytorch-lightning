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

import torch
from pytorch_lightning.core import LightningModule
from pytorch_lightning.utilities import AMPType
from pytorch_lightning.accelerators.base_backend import Accelerator

try:
    from apex import amp
except ImportError:
    amp = None


class GPUBackend(Accelerator):
    amp_backend: AMPType

    def __init__(self, trainer):
        super().__init__(trainer)

    def setup(self, model):

        # call setup
        self.trainer.call_setup_hook(model)

        torch.cuda.set_device(self.trainer.root_gpu)
        model.cuda(self.trainer.root_gpu)

        # CHOOSE OPTIMIZER
        # allow for lr schedulers as well
        optimizers, lr_schedulers, optimizer_frequencies = self.trainer.init_optimizers(model)
        self.trainer.optimizers = optimizers
        self.trainer.lr_schedulers = lr_schedulers
        self.trainer.optimizer_frequencies = optimizer_frequencies

        if self.trainer.amp_backend == AMPType.APEX:
            model = self._setup_nvidia_apex(model)

        self.trainer.model = model

    def train(self):
        model = self.trainer.model

        # set up training routine
        self.trainer.setup_training(model)

        # train or test
        results = self.trainer.train_or_test()

        return results

    def training_step(self, args):
        if self.trainer.amp_backend == AMPType.NATIVE:
            with torch.cuda.amp.autocast():
                output = self.__training_step(args)
        else:
            output = self.__training_step(args)

        return output

    def __training_step(self, args):
        batch = args[0]
        batch = self.to_device(batch)
        args[0] = batch
        output = self.trainer.model.training_step(*args)
        return output

    def validation_step(self, args):
        if self.trainer.amp_backend == AMPType.NATIVE:
            with torch.cuda.amp.autocast():
                output = self.__validation_step(args)
        else:
            output = self.__validation_step(args)

        return output

    def __validation_step(self, args):
        batch = args[0]
        batch = self.to_device(batch)
        args[0] = batch
        output = self.trainer.model.validation_step(*args)
        return output

    def test_step(self, args):
        if self.trainer.amp_backend == AMPType.NATIVE:
            with torch.cuda.amp.autocast():
                output = self.__test_step(args)
        else:
            output = self.__test_step(args)

        return output

    def __test_step(self, args):
        batch = args[0]
        batch = self.to_device(batch)
        args[0] = batch
        output = self.trainer.model.test_step(*args)
        return output

    def to_device(self, batch):
        gpu_id = 0
        if isinstance(self.trainer.data_parallel_device_ids, list):
            gpu_id = self.trainer.data_parallel_device_ids[0]

        # Don't copy the batch since there is a single gpu that the batch could
        # be referenced from and if there are multiple optimizers the batch will
        # wind up copying it to the same device repeatedly.
        return self.batch_to_device(batch, gpu_id)

    def _setup_nvidia_apex(self, model: LightningModule):
        model, optimizers = model.configure_apex(amp, model, self.trainer.optimizers, self.trainer.amp_level)
        self.trainer.optimizers = optimizers
        self.trainer.reinit_scheduler_properties(self.trainer.optimizers, self.trainer.lr_schedulers)
        return model
