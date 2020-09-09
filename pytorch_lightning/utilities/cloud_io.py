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

import io
from distutils.version import LooseVersion
from typing import Union
from pathlib import Path
from urllib.parse import urlparse
import torch
import fsspec


pathlike = Union[Path, str]


def load(path_or_url: str, map_location=None):
    if urlparse(path_or_url).scheme == "" or Path(path_or_url).drive:  # no scheme or with a drive letter
        return torch.load(path_or_url, map_location=map_location)
    return torch.hub.load_state_dict_from_url(path_or_url, map_location=map_location)


def get_filesystem(path: pathlike):
    path = str(path)
    if "://" in path:
        # use the fileystem from the protocol specified
        return fsspec.filesystem(path.split(":", 1)[0])
    else:
        # use local filesystem
        return fsspec.filesystem("file")


def atomic_save(checkpoint, filepath: str):
    """Saves a checkpoint atomically, avoiding the creation of incomplete checkpoints.

    Args:
        checkpoint: The object to save.
            Built to be used with the ``dump_checkpoint`` method, but can deal with anything which ``torch.save``
            accepts.
        filepath: The path to which the checkpoint will be saved.
            This points to the file that the checkpoint will be stored in.
    """
    bytesbuffer = io.BytesIO()
    # Can't use the new zipfile serialization for 1.6.0 because there's a bug in
    # torch.hub.load_state_dict_from_url() that prevents it from loading the new files.
    # More details can be found here: https://github.com/pytorch/pytorch/issues/42239
    if LooseVersion(torch.__version__).version[:3] == [1, 6, 0]:
        torch.save(checkpoint, bytesbuffer, _use_new_zipfile_serialization=False)
    else:
        torch.save(checkpoint, bytesbuffer)
    with fsspec.open(filepath, "wb") as f:
        f.write(bytesbuffer.getvalue())
