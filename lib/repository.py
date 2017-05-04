# Copyright (C) IBM Corp. 2016.
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

import logging
import os
import urlparse
import utils

import git
import gitdb

from lib import config
from lib import exception

CONF = config.get_config().CONF
LOG = logging.getLogger(__name__)
MAIN_REMOTE_NAME = "origin"


class PushError(Exception):

    def __init__(self, push_info):
        message = ("Error pushing to remote reference %s"
                   % push_info.remote_ref.name)
        super(PushError, self).__init__(message)


def get_git_repository(remote_repo_url, parent_dir_path, name=None):
    """
    Get a local git repository located in a subdirectory of the parent
    directory, named after the file name of the URL path (git default),
    updating the main remote URL, if needed.
    If the local repository does not exist, clone it from the remote
    URL.

    Args:
        remote_repo_url (str): URL to remote Git repository
        parent_dir_path (str): path to parent directory of the repository
            directory
        name (str): name of the repository directory. Leave empty to infer the
            name from the URL
    """
    # infer git repository name from its URL
    url_parts = urlparse.urlparse(remote_repo_url)
    if not name:
        name = os.path.basename(os.path.splitext(url_parts.path)[0])

    repo_path = os.path.join(parent_dir_path, name)
    try:
        repo = GitRepository(repo_path)
    except git.exc.InvalidGitRepositoryError:
        # This exception will be thrown if the Git repository has an
        # invalid format, which could be due to the repository
        # changing from SVN to Git, for example.  A way to proceed is
        # removing the directory and cloning from scratch.
        message = ("The Git repository at '{repo_path}' has an invalid format. "
                   "If you are unsure of what to do, remove it and try again."
                   .format(repo_path=repo_path))
        LOG.error(message)
        raise
    except git.exc.NoSuchPathError:
        message = ("Repository path '{repo_path}' does not exist."
                   .format(repo_path=repo_path))
        LOG.debug(message)
        proxy = CONF.get('http_proxy')
        return GitRepository.clone_from(remote_repo_url, repo_path, proxy=proxy)
    else:
        repo.force_create_remote(MAIN_REMOTE_NAME, remote_repo_url)
        return repo


def get_svn_repository(remote_repo_url, repo_path):
    """
    Get a local subversion repository located in a directory.
    If it does not exist, check it out from the specified URL.
    """
    if os.path.exists(repo_path):
        return SvnRepository(remote_repo_url, repo_path)
    else:
        # TODO: setup HTTP proxy by editing ~/.subversion/servers
        return SvnRepository.checkout_from(remote_repo_url,
                                           repo_path)


class GitRepository(git.Repo):

    @classmethod
    def clone_from(cls, remote_repo_url, repo_path, proxy=None, *args, **kwargs):
        """
        Clone a repository from a remote URL into a local path.
        """
        LOG.info("Cloning repository from '%s' into '%s'" %
                 (remote_repo_url, repo_path))
        try:
            if proxy:
                git_cmd = git.cmd.Git()
                git_cmd.execute(['git',
                                 '-c',
                                 "http.proxy='{}'".format(proxy),
                                 'clone',
                                 remote_repo_url,
                                 repo_path])
                return GitRepository(repo_path)
            else:
                return super(GitRepository, cls).clone_from(
                    remote_repo_url, repo_path, *args, **kwargs)
        except git.exc.GitCommandError:
            message = "Failed to clone repository"
            LOG.exception(message)
            raise exception.RepositoryError(message=message)

    def __init__(self, repo_path, *args, **kwargs):
        super(GitRepository, self).__init__(repo_path, *args, **kwargs)
        LOG.info("Found existent repository at destination path %s" % repo_path)

    @property
    def name(self):
        return os.path.basename(self.working_tree_dir)

    def checkout(self, ref_name, refspecs=None):
        """
        Check out the reference name, resetting the index state.
        The reference may be a branch, tag or commit.

        Args:
            ref_name (str): name of the reference. May be a branch, tag,
                commit ID, etc.
            refspecs ([str]): pattern mappings from remote references to
                local references. Refer to Git documentation at
                https://git-scm.com/book/id/v2/Git-Internals-The-Refspec
        """
        LOG.info("%(name)s: Fetching repository remote %(remote)s"
                 % dict(name=self.name, remote=MAIN_REMOTE_NAME))
        if refspecs is not None:
            LOG.debug("Using custom ref specs %s" % refspecs)
        main_remote = self.remote(MAIN_REMOTE_NAME)
        try:
            main_remote.fetch(refspecs)
        except git.exc.GitCommandError:
            LOG.error("Failed to fetch %s remote for %s"
                      % (MAIN_REMOTE_NAME, self.name))
            raise

        commit_id = self._get_reference(ref_name)
        LOG.info("%(name)s: Checking out reference %(ref)s pointing to commit "
                 "%(commit)s"
                 % dict(name=self.name, ref=ref_name, commit=commit_id))
        self.head.reference = commit_id
        try:
            self.head.reset(index=True, working_tree=True)
        except git.exc.GitCommandError:
            message = ("Could not find reference %s at %s repository"
                       % (ref_name, self.name))
            LOG.exception(message)
            raise exception.RepositoryError(message=message)

        self._update_submodules()

    def _get_reference(self, ref_name):
        """
        Get repository commit based on a reference name (branch, tag,
        commit ID). Remote references have higher priority than local
        references.
        """
        refs_names = []
        for remote in self.remotes:
            refs_names.append(os.path.join(remote.name, ref_name))
        refs_names.append(ref_name)
        for ref_name in refs_names:
            try:
                return self.commit(ref_name)
            except gitdb.exc.BadName:
                pass
        else:
            raise exception.RepositoryError(
                message="Reference '%s' not found in repository" % ref_name)

    def _update_submodules(self):
        """
        Update repository submodules, initializing them if needed.
        """
        for submodule in self.submodules:
            LOG.info("Updating submodule %(name)s from %(url)s"
                     % dict(name=submodule.name, url=submodule.url))
            submodule.update(init=True)

    def archive(self, archive_name, build_dir):
        """
        Archive repository and its submodules into a single compressed
        file.

        Args:
            archive_name (str): prefix of the resulting archive file
                name
            build_dir (str): path to the directory to place the archive
                file
        """
        archive_file_path = os.path.join(build_dir, archive_name + ".tar")

        LOG.info("Archiving {name} into {file}"
                 .format(name=self.name, file=archive_file_path))
        with open(archive_file_path, "wb") as archive_file:
            super(GitRepository, self).archive(
                archive_file, prefix=archive_name + "/", format="tar")

        # Generate one tar file for each submodule
        submodules_archives_paths = []
        for submodule in self.submodules:
            submodule_archive_file_path = os.path.join(
                build_dir, "%s-%s.tar" % (
                    archive_name, submodule.name.replace("/", "_")))
            LOG.info("Archiving submodule {name} into {file}".format(
                name=submodule.name, file=submodule_archive_file_path))
            with open(submodule_archive_file_path, "wb") as archive_file:
                submodule.module().archive(archive_file, prefix=os.path.join(
                    archive_name, submodule.path) + "/", format="tar")
            submodules_archives_paths.append(submodule_archive_file_path)

        if submodules_archives_paths:
            LOG.info("Concatenating {name} archive with submodules"
                     .format(name=self.name))
            for submodule_archive_path in submodules_archives_paths:
                # The tar --concatenate option has a bug, producing an
                # undesired result when more than two files are
                # concatenated:
                # https://lists.gnu.org/archive/html/bug-tar/2008-08/msg00002.html
                cmd = "tar --concatenate --file %s %s" % (
                    archive_file_path, submodule_archive_path)
                utils.run_command(cmd)

        compressed_archive_file_path = archive_file_path + ".gz"
        LOG.info("Compressing {name} archive into {file}"
                 .format(name=self.name, file=compressed_archive_file_path))
        cmd = "gzip --fast %s" % archive_file_path
        utils.run_command(cmd)
        return compressed_archive_file_path

    def commit_changes(self, commit_message, committer_name, committer_email):
        """
        Commit all changes made to the repository.

        Args:
            commit_message (str): message describing the commit
            committer_name (str): committer name
            committer_email (str): committer email
        """
        LOG.info("Adding files to repository index")
        self.index.add(["*"])

        LOG.info("Committing changes to local repository")
        actor = git.Actor(committer_name, committer_email)
        self.index.commit(commit_message, author=actor, committer=actor)

    def push_head_commits(self, remote_repo_url, remote_repo_branch):
        """
        Push commits from local Git repository head to the remote Git
        repository, using the system's configured SSH credentials.

        Args:
            remote_repo_url (str): remote git repository URL
            remote_repo_branch (str): remote git repository branch

        Raises:
            repository.PushError: if push fails
        """
        REPO_REMOTE_NAME = "push-remote"
        self.force_create_remote(REPO_REMOTE_NAME, remote_repo_url)

        LOG.info("Pushing changes to remote repository branch '{}'"
                 .format(remote_repo_branch))
        remote = self.remote(REPO_REMOTE_NAME)
        refspec = "HEAD:refs/heads/{}".format(remote_repo_branch)
        push_info = remote.push(refspec=refspec)[0]
        LOG.debug("Push result: {}".format(push_info.summary))
        if git.PushInfo.ERROR & push_info.flags:
            raise PushError(push_info)

    def force_create_remote(self, name, url):
        """
        Create a remote, replacing a previous one with the same name.

        Args:
            name (str): remote name
            url (str): remote URL
        """
        if any(remote.name == name for remote in self.remotes):
            previous_url = self.remotes[name].url
            if previous_url != url:
                LOG.debug("Removing previous {name}'s repository remote with "
                          "URL '{previous_url}'"
                          .format(name=name, previous_url=previous_url))
                self.delete_remote(name)
        if not any(remote.name == name for remote in self.remotes):
            LOG.debug("Creating {name}'s repository remote with URL '{url}'"
                      .format(name=name, url=url))
            self.create_remote(name, url)


class SvnRepository():

    @classmethod
    def checkout_from(cls, remote_repo_url, repo_path):
        """
        Checkout a repository from a remote URL into a local path.
        """
        LOG.info("Checking out repository from '%s' into '%s'" %
                 (remote_repo_url, repo_path))

        command = 'svn checkout '

        proxy = CONF.get('http_proxy')

        if proxy:
            url = urlparse.urlparse(proxy)
            host = url.scheme + '://' + url.hostname
            port = url.port
            options = ("servers:global:http-proxy-host='%s'" % host,
                       "servers:global:http-proxy-port='%s'" % port)

            proxy_conf = ['--config-option ' + option for option in options]

            command += ' '.join(proxy_conf) + ' '

        command += '%(remote_repo_url)s %(local_target_path)s' % \
                   {'remote_repo_url': remote_repo_url,
                    'local_target_path': repo_path}
        try:
            utils.run_command(command)
            return SvnRepository(remote_repo_url, repo_path)
        except:
            message = "Failed to clone repository"
            LOG.exception(message)
            raise exception.RepositoryError(message=message)

    def __init__(self, remote_repo_url, local_repo_path):
        self.url = remote_repo_url
        self.working_copy_dir = local_repo_path
        LOG.info("Found existent repository at destination path %s" % local_repo_path)

    @property
    def name(self):
        return os.path.basename(self.working_copy_dir)

    def checkout(self, revision):
        """
        Check out a revision.
        """
        LOG.info("%(name)s: Updating svn repository"
                 % dict(name=self.name))
        try:
            utils.run_command("svn update", cwd=self.working_copy_dir)
        except:
            LOG.debug("%(name)s: Failed to update svn repository"
                      % dict(name=self.name))
            pass
        else:
            LOG.info("%(name)s: Updated svn repository" % dict(name=self.name))

        LOG.info("%(name)s: Checking out revision %(revision)s"
                 % dict(name=self.name, revision=revision))
        try:
            utils.run_command("svn checkout %(repo_url)s@%(revision)s ." %
                dict(repo_url=self.url, revision=revision),
                cwd=self.working_copy_dir)
        except:
            message = ("Could not find revision %s at %s repository"
                       % (revision, self.name))
            LOG.exception(message)
            raise exception.RepositoryError(message=message)
