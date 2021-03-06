import sys
reload(sys)
sys.setdefaultencoding('utf8')
import os
import subprocess
import time
import ConfigParser
import shlex
import shutil
import logging

sys.path.append(os.path.join(os.path.dirname(__file__), 'utils'))
sys.path.append(os.path.dirname(__file__))

import pystache
import git

from twisted.internet import threads, reactor, defer
from config import BrickConfig
from projects import Projects

from builder_rpm import BuilderRpm
from builder_deb import BuilderDeb
from build_options import BuildOptions
from dreque import Dreque

config = BrickConfig()
redis_server = config.get('redis', 'redis-server')
log_file = config.get('log', 'file')

queue = Dreque(redis_server)

logging.basicConfig(filename=log_file, level=logging.DEBUG)
log = logging.getLogger('builder')

@defer.inlineCallbacks
def build_project(kargs):
    builder = Builder(kargs['project'])
    kargs.pop('project')
    yield builder.build_project(**kargs)

class Builder(object):
    def __init__(self, project):
        self.project = Projects(project)
        self.templates_dir = BrickConfig().get('workspace', 'template_dir')
        self.git = git.Git(self.project)
        self.distro = 'debian_squeeze'
        self.build_system = BrickConfig().get('build', 'system')
        self.build_options = BuildOptions(self.git.workdir)

        self.schroot_dir = BrickConfig().get('schroot', 'dir')
        self.workspace = "%s/%s/%s" % (
            self.schroot_dir,
            self.project.name,
            BrickConfig().get('workspace', 'dir')
        )

        if self.build_system == 'rpm':
            self.mod_install_cmd = self.project.install_cmd.replace(
                'BUILDROOT', '%{buildroot}'
            )
        elif self.build_system == 'deb' or self.build_system == None:
            self.chbootstrap = ('/usr/bin/cdebootstrap --arch=%s stable %s/%s %s' %
                                    (BrickConfig().get('schroot', 'arch', self.schroot_dir, 'template_debian',
                                     BrickConfig().get('schroot', 'mirror_%s' % self.distro))
                                )
            self.mod_install_cmd = self.project.install_cmd.replace(
                'BUILDROOT', 'debian/tmp'
            )
            if not os.path.isdir("%s/%s" % (self.schroot_dir, 'template_%s' % self.distro)):
                chb = self._exec(shlex.split(self.chbootstrap))
                chb.wait()

        if not os.path.isdir(self.workspace):
            os.makedirs(self.workspace)

        if not os.path.isdir(os.path.join(self.workspace, 'log')):
            os.makedirs(os.path.join(self.workspace, 'log'))

        self.stdout = None
        self.stderr = self.stdout

    def _exec(self, cmd, *args, **kwargs):
        if (not hasattr(self, 'chbootstrap')) or ('/usr/bin/cdebootstrap' in cmd):
            log.info(cmd)
            return subprocess.Popen(cmd, *args, **kwargs)
        else:
            return subprocess.Popen(['schroot', '-c', self.project.name, '--'] + cmd, *args, **kwargs)

    def build_project(self, branch=None, release=None, version=None, commit=None):

        if not self.project.is_building():
            self.project.start_building()
            try:
                self.workdir = "%s-%s" % (self.git.workdir, release)
                shutil.copytree(self.git.workdir, self.workdir)

                if self.build_system == 'rpm':
                    self.package_builder = BuilderRpm(self)
                elif self.build_system == 'deb':
                    if not os.path.isdir("%s/%s" % (self.schroot_dir, self.project.name)):
                        shutil.copytree("%s/%s" % (self.schroot_dir, 'template_%s' % self.distro),
                                        "%s/%s" % (self.schroot_dir, self.projet.name))
                    self.package_builder = BuilderDeb(self)

                os.chdir(self.workdir)
                self.git.workdir = self.workdir
                self.git.checkout_branch(branch)

                if release == 'experimental' and self.build_options.changelog:
                    self.git.checkout_branch(branch)
                    self.package_builder.build(branch, release)
                    self.package_builder.upload(branch)
                if release != None and commit != None:
                    self.git.checkout_tag(commit)
                    self.package_builder.build(branch, force_version=version, force_release=release)
                    self.package_builder.upload(release)
                else:
                    self.project.last_tag(release, self.git.last_tag(release))
                    self.git.checkout_tag(self.project.last_tag(release))
                    self.package_builder.build(branch, self.project.last_tag(release))
                    self.package_builder.upload(release)
                self.git.checkout_branch('master')

                shutil.rmtree(self.workdir)

            except Exception, e:
                log.exception("build failed: %s" % repr(e))
            finally:
                self.project.stop_building()
                shutil.rmtree(self.workdir)

