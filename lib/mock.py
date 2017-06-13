# Copyright (C) IBM Corp. 2017.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import fcntl
import os

from lib import config
from lib import utils

CONF = config.get_config().CONF

LOCK_FILE_NAME = "mock.lock"
LOCK_FILE_PATH = os.path.join(CONF.get('work_dir'), LOCK_FILE_NAME)

class Mock(object):

    def __init__(self, config_file, unique_extension):
        """
        Constructor

        Args:
            config_file (str): configuration file path
            unique_extension (str): unique extension to append to chroot
                directory name
        """
        self.binary_file = CONF.get('mock_binary')
        self.config_file = config_file
        self.extra_args = CONF.get('mock_args') or ""
        self.unique_extension = unique_extension

        self.common_mock_args = [
            self.binary_file, "-r", self.config_file, self.extra_args,
            "--uniqueext", self.unique_extension]

    def run_command(self, cmd):
        """
        Run mock command using arguments passed to the constructor.

        Args:
            cmd (str): mock command to execute
        """
        cmd = " ".join(self.common_mock_args + [cmd])
        utils.run_command(cmd)

    def initialize(self):
        """
        Initializes the configured chroot by discarding previous caches
        and installing the essential packages. This setup needs to be
        done only once for a given configuration.
        This method is thread safe.
        """
        lock_file = open(LOCK_FILE_PATH, "w")
        fcntl.lockf(lock_file, fcntl.LOCK_EX)
        self.run_command("--scrub all")
        self.run_command("--init")
        fcntl.lockf(lock_file, fcntl.LOCK_UN)
