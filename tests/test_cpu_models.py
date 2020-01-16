import warnings

import torch

import tests.utils as tutils
from pytorch_lightning import Trainer, data_loader
from pytorch_lightning.callbacks import (
    EarlyStopping,
)
from pytorch_lightning.testing import (
    LightningTestModel,
    LightningTestModelBase,
    LightningTestMixin,
)


def test_early_stopping_cpu_model(tmpdir):
    """Test each of the trainer options."""
    tutils.reset_seed()

    stopping = EarlyStopping(monitor='val_loss', min_delta=0.1)
    trainer_options = dict(
        default_save_path=tmpdir,
        early_stop_callback=stopping,
        gradient_clip_val=1.0,
        overfit_pct=0.20,
        track_grad_norm=2,
        print_nan_grads=True,
        show_progress_bar=True,
        logger=tutils.get_test_tube_logger(tmpdir),
        train_percent_check=0.1,
        val_percent_check=0.1
    )

    model, hparams = tutils.get_model()
    tutils.run_model_test(trainer_options, model, on_gpu=False)

    # test freeze on cpu
    model.freeze()
    model.unfreeze()


def test_lbfgs_cpu_model(tmpdir):
    """Test each of the trainer options."""
    tutils.reset_seed()

    trainer_options = dict(
        default_save_path=tmpdir,
        max_epochs=1,
        print_nan_grads=True,
        show_progress_bar=False,
        weights_summary='top',
        train_percent_check=1.0,
        val_percent_check=0.2
    )

    model, hparams = tutils.get_model(use_test_model=True, lbfgs=True)
    tutils.run_model_test_no_loggers(trainer_options, model, min_acc=0.30)


def test_default_logger_callbacks_cpu_model(tmpdir):
    """Test each of the trainer options."""
    tutils.reset_seed()

    trainer_options = dict(
        default_save_path=tmpdir,
        max_epochs=1,
        gradient_clip_val=1.0,
        overfit_pct=0.20,
        print_nan_grads=True,
        show_progress_bar=False,
        train_percent_check=0.01,
        val_percent_check=0.01
    )

    model, hparams = tutils.get_model()
    tutils.run_model_test_no_loggers(trainer_options, model)

    # test freeze on cpu
    model.freeze()
    model.unfreeze()


def test_running_test_after_fitting(tmpdir):
    """Verify test() on fitted model."""
    tutils.reset_seed()

    hparams = tutils.get_hparams()
    model = LightningTestModel(hparams)

    # logger file to get meta
    logger = tutils.get_test_tube_logger(tmpdir, False)

    # logger file to get weights
    checkpoint = tutils.init_checkpoint_callback(logger)

    trainer_options = dict(
        default_save_path=tmpdir,
        show_progress_bar=False,
        max_epochs=4,
        train_percent_check=0.4,
        val_percent_check=0.2,
        test_percent_check=0.2,
        checkpoint_callback=checkpoint,
        logger=logger
    )

    # fit model
    trainer = Trainer(**trainer_options)
    result = trainer.fit(model)

    assert result == 1, 'training failed to complete'

    trainer.test()

    # test we have good test accuracy
    tutils.assert_ok_test_acc(trainer)


def test_running_test_without_val(tmpdir):
    """Verify `test()` works on a model with no `val_loader`."""
    tutils.reset_seed()

    class CurrentTestModel(LightningTestMixin, LightningTestModelBase):
        pass

    hparams = tutils.get_hparams()
    model = CurrentTestModel(hparams)

    # logger file to get meta
    logger = tutils.get_test_tube_logger(tmpdir, False)

    # logger file to get weights
    checkpoint = tutils.init_checkpoint_callback(logger)

    trainer_options = dict(
        show_progress_bar=False,
        max_epochs=1,
        train_percent_check=0.4,
        val_percent_check=0.2,
        test_percent_check=0.2,
        checkpoint_callback=checkpoint,
        logger=logger
    )

    # fit model
    trainer = Trainer(**trainer_options)
    result = trainer.fit(model)

    assert result == 1, 'training failed to complete'

    trainer.test()

    # test we have good test accuracy
    tutils.assert_ok_test_acc(trainer)


def test_single_gpu_batch_parse():
    tutils.reset_seed()

    if not tutils.can_run_gpu_test():
        return

    trainer = Trainer()

    # batch is just a tensor
    batch = torch.rand(2, 3)
    batch = trainer.transfer_batch_to_gpu(batch, 0)
    assert batch.device.index == 0 and batch.type() == 'torch.cuda.FloatTensor'

    # tensor list
    batch = [torch.rand(2, 3), torch.rand(2, 3)]
    batch = trainer.transfer_batch_to_gpu(batch, 0)
    assert batch[0].device.index == 0 and batch[0].type() == 'torch.cuda.FloatTensor'
    assert batch[1].device.index == 0 and batch[1].type() == 'torch.cuda.FloatTensor'

    # tensor list of lists
    batch = [[torch.rand(2, 3), torch.rand(2, 3)]]
    batch = trainer.transfer_batch_to_gpu(batch, 0)
    assert batch[0][0].device.index == 0 and batch[0][0].type() == 'torch.cuda.FloatTensor'
    assert batch[0][1].device.index == 0 and batch[0][1].type() == 'torch.cuda.FloatTensor'

    # tensor dict
    batch = [{'a': torch.rand(2, 3), 'b': torch.rand(2, 3)}]
    batch = trainer.transfer_batch_to_gpu(batch, 0)
    assert batch[0]['a'].device.index == 0 and batch[0]['a'].type() == 'torch.cuda.FloatTensor'
    assert batch[0]['b'].device.index == 0 and batch[0]['b'].type() == 'torch.cuda.FloatTensor'

    # tuple of tensor list and list of tensor dict
    batch = ([torch.rand(2, 3) for _ in range(2)],
             [{'a': torch.rand(2, 3), 'b': torch.rand(2, 3)} for _ in range(2)])
    batch = trainer.transfer_batch_to_gpu(batch, 0)
    assert batch[0][0].device.index == 0 and batch[0][0].type() == 'torch.cuda.FloatTensor'

    assert batch[1][0]['a'].device.index == 0
    assert batch[1][0]['a'].type() == 'torch.cuda.FloatTensor'

    assert batch[1][0]['b'].device.index == 0
    assert batch[1][0]['b'].type() == 'torch.cuda.FloatTensor'


def test_simple_cpu(tmpdir):
    """Verify continue training session on CPU."""
    tutils.reset_seed()

    hparams = tutils.get_hparams()
    model = LightningTestModel(hparams)

    # logger file to get meta
    trainer_options = dict(
        default_save_path=tmpdir,
        max_epochs=1,
        val_percent_check=0.1,
        train_percent_check=0.1,
    )

    # fit model
    trainer = Trainer(**trainer_options)
    result = trainer.fit(model)

    # traning complete
    assert result == 1, 'amp + ddp model failed to complete'


def test_cpu_model(tmpdir):
    """Make sure model trains on CPU."""
    tutils.reset_seed()

    trainer_options = dict(
        default_save_path=tmpdir,
        show_progress_bar=False,
        logger=tutils.get_test_tube_logger(tmpdir),
        max_epochs=1,
        train_percent_check=0.4,
        val_percent_check=0.4
    )

    model, hparams = tutils.get_model()

    tutils.run_model_test(trainer_options, model, on_gpu=False)


def test_all_features_cpu_model(tmpdir):
    """Test each of the trainer options."""
    tutils.reset_seed()

    trainer_options = dict(
        default_save_path=tmpdir,
        gradient_clip_val=1.0,
        overfit_pct=0.20,
        track_grad_norm=2,
        print_nan_grads=True,
        show_progress_bar=False,
        logger=tutils.get_test_tube_logger(tmpdir),
        accumulate_grad_batches=2,
        max_epochs=1,
        train_percent_check=0.4,
        val_percent_check=0.4
    )

    model, hparams = tutils.get_model()
    tutils.run_model_test(trainer_options, model, on_gpu=False)


def test_tbptt_cpu_model(tmpdir):
    """Test truncated back propagation through time works."""
    tutils.reset_seed()

    truncated_bptt_steps = 2
    sequence_size = 30
    batch_size = 30

    x_seq = torch.rand(batch_size, sequence_size, 1)
    y_seq_list = torch.rand(batch_size, sequence_size, 1).tolist()

    class MockSeq2SeqDataset(torch.utils.data.Dataset):
        def __getitem__(self, i):
            return x_seq, y_seq_list

        def __len__(self):
            return 1

    class BpttTestModel(LightningTestModelBase):
        def __init__(self, hparams):
            super().__init__(hparams)
            self.test_hidden = None

        def training_step(self, batch, batch_idx, hiddens):
            assert hiddens == self.test_hidden, "Hidden state not persistent between tbptt steps"
            self.test_hidden = torch.rand(1)

            x_tensor, y_list = batch
            assert x_tensor.shape[1] == truncated_bptt_steps, "tbptt split Tensor failed"

            y_tensor = torch.tensor(y_list, dtype=x_tensor.dtype)
            assert y_tensor.shape[1] == truncated_bptt_steps, "tbptt split list failed"

            pred = self.forward(x_tensor.view(batch_size, truncated_bptt_steps))
            loss_val = torch.nn.functional.mse_loss(
                pred, y_tensor.view(batch_size, truncated_bptt_steps))
            return {
                'loss': loss_val,
                'hiddens': self.test_hidden,
            }

        @data_loader
        def train_dataloader(self):
            return torch.utils.data.DataLoader(
                dataset=MockSeq2SeqDataset(),
                batch_size=batch_size,
                shuffle=False,
                sampler=None,
            )

    trainer_options = dict(
        default_save_path=tmpdir,
        max_epochs=1,
        truncated_bptt_steps=truncated_bptt_steps,
        val_percent_check=0,
        weights_summary=None,
    )

    hparams = tutils.get_hparams()
    hparams.batch_size = batch_size
    hparams.in_features = truncated_bptt_steps
    hparams.hidden_dim = truncated_bptt_steps
    hparams.out_features = truncated_bptt_steps

    model = BpttTestModel(hparams)

    # fit model
    trainer = Trainer(**trainer_options)
    result = trainer.fit(model)

    assert result == 1, 'training failed to complete'


def test_single_gpu_model(tmpdir):
    """Make sure single GPU works (DP mode)."""
    tutils.reset_seed()

    if not torch.cuda.is_available():
        warnings.warn('test_single_gpu_model cannot run.'
                      ' Rerun on a GPU node to run this test')
        return
    model, hparams = tutils.get_model()

    trainer_options = dict(
        default_save_path=tmpdir,
        show_progress_bar=False,
        max_epochs=1,
        train_percent_check=0.1,
        val_percent_check=0.1,
        gpus=1
    )

    tutils.run_model_test(trainer_options, model)


# if __name__ == '__main__':
#     pytest.main([__file__])
