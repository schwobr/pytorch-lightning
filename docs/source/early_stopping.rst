.. testsetup:: *

    from pytorch_lightning.trainer.trainer import Trainer
    from pytorch_lightning.callbacks.early_stopping import EarlyStopping

.. _early-stopping:

Early stopping
==============

Stopping an epoch early
-----------------------
You can stop an epoch early by overriding :meth:`~pytorch_lightning.core.lightning.LightningModule.on_batch_start` to return `-1` when some condition is met.

If you do this repeatedly, for every epoch you had originally requested, then this will stop your entire run.

----------

Default Epoch End Callback Behavior
-----------------------------------
By default early stopping will be enabled if `'val_loss'`
is found in :meth:`~pytorch_lightning.core.lightning.LightningModule.validation_epoch_end`'s
return dict. Otherwise training will proceed with early stopping disabled.

----------

Enable Early Stopping using the EarlyStopping Callback
------------------------------------------------------
The
:class:`~pytorch_lightning.callbacks.early_stopping.EarlyStopping`
callback can be used to monitor a validation metric and stop the training when no improvement is observed.

There are two ways to enable the EarlyStopping callback:

-   Set `early_stop_callback=True`.
    The callback will look for 'val_loss' in the dict returned by 
    :meth:`~pytorch_lightning.core.lightning.LightningModule.validation_epoch_end`
    and raise an error if `val_loss` is not present.

    .. testcode::

        trainer = Trainer(early_stop_callback=True)

-   Create the callback object and pass it to the trainer.
    This allows for further customization.

    .. testcode::

        early_stop_callback = EarlyStopping(
           monitor='val_accuracy',
           min_delta=0.00,
           patience=3,
           verbose=False,
           mode='max'
        )
        trainer = Trainer(early_stop_callback=early_stop_callback)

In case you need early stopping in a different part of training, subclass EarlyStopping
and change where it is called:

.. testcode::

    class MyEarlyStopping(EarlyStopping):

        def on_validation_end(self, trainer, pl_module):
            # override this to disable early stopping at the end of val loop
            pass

        def on_train_end(self, trainer, pl_module):
            # instead, do it at the end of training loop
            self._run_early_stopping_check(trainer, pl_module)

.. note::
   The EarlyStopping callback runs at the end of every validation epoch,
   which, under the default configuration, happen after every training epoch.
   However, the frequency of validation can be modified by setting various parameters
   on the :class:`~pytorch_lightning.trainer.trainer.Trainer`,
   for example :paramref:`~pytorch_lightning.trainer.trainer.Trainer.check_val_every_n_epoch`
   and :paramref:`~pytorch_lightning.trainer.trainer.Trainer.val_check_interval`.
   It must be noted that the `patience` parameter counts the number of
   validation epochs with no improvement, and not the number of training epochs.
   Therefore, with parameters `check_val_every_n_epoch=10` and `patience=3`, the trainer
   will perform at least 40 training epochs before being stopped.

.. seealso::
    - :class:`~pytorch_lightning.trainer.trainer.Trainer`
    - :class:`~pytorch_lightning.callbacks.early_stopping.EarlyStopping`

----------

Disable Early Stopping with callbacks on epoch end
--------------------------------------------------
To disable early stopping pass ``False`` to the
:paramref:`~pytorch_lightning.trainer.trainer.Trainer.early_stop_callback`.
Note that ``None`` will not disable early stopping but will lead to the
default behaviour.

.. seealso::
    - :class:`~pytorch_lightning.trainer.trainer.Trainer`
    - :class:`~pytorch_lightning.callbacks.early_stopping.EarlyStopping`
