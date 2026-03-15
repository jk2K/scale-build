import contextlib
import json
import os
import re
import shlex
import shutil

from datetime import datetime
from scale_build.config import BUILD_TIME, VERSION, TRUENAS_VENDOR
from scale_build.exceptions import CallError
from scale_build.utils.environment import APT_ENV
from scale_build.utils.manifest import get_truenas_train, get_release_code_name, get_secret_env, get_manifest
from scale_build.utils.run import run
from scale_build.utils.paths import PKG_DIR


class BuildPackageMixin:

    def run_in_chroot(self, command, exception_message=None):
        run(
            f'chroot {self.dpkg_overlay} /bin/bash -c {shlex.quote(command)}', shell=True,
            exception_msg=exception_message,
            env=self._get_build_env() | self._get_chroot_env()
        )

    @property
    def source_in_chroot(self):
        return os.path.join(self.dpkg_overlay, 'dpkg-src')

    @property
    def package_source_with_chroot(self):
        return os.path.join(self.dpkg_overlay, self.package_source)

    @property
    def package_source(self):
        return os.path.join(*filter(bool, ('dpkg-src', self.subdir)))

    def build(self):
        # The flow is the following steps
        # 1) Bootstrap a directory for package
        # 2) Delete existing overlayfs
        # 3) Create an overlayfs
        # 4) Clean previous packages
        # 5) Apt update
        # 6) Install linux custom headers/image for kernel based packages
        # 7) Execute relevant predep commands
        # 8) Install build depends
        # 9) Execute relevant prebuild commands
        # 10) Generate version
        # 11) Execute relevant building commands
        # 12) Save
        self.delete_overlayfs()
        self.setup_chroot_basedir()
        self.make_overlayfs()
        self.clean_previous_packages()
        self._build_impl()

    def _get_build_env(self):
        env = {
            **os.environ,
            **APT_ENV,
            **self.env,
        }
        env.update(self.ccache_env(env))
        return env

    def _get_chroot_env(self):
        env = {
            'RELEASE_VERSION': VERSION,
        }
        secrets = get_secret_env()
        for k in filter(lambda k: k in secrets, self.secret_env):
            env[k] = secrets[k]
        return env

    def _get_debian_info(self):
        """Read Debian version and codename from the build chroot filesystem.

        Returns a dict with 'version' (e.g. '13') and 'codename' (e.g. 'trixie').
        """
        # Read major version from /etc/debian_version (e.g. "13", "13.1", "trixie/sid")
        debian_version_file = os.path.join(self.dpkg_overlay, 'etc/debian_version')
        with open(debian_version_file, 'r') as f:
            raw = f.read().strip()

        # For testing/unstable releases, debian_version may contain "trixie/sid"
        # so we also parse /etc/os-release for the codename
        if raw[0].isdigit():
            debian_version = raw.split('.')[0]
        else:
            debian_version = None

        # Parse /etc/os-release for VERSION_CODENAME and VERSION_ID
        os_release_file = os.path.join(self.dpkg_overlay, 'etc/os-release')
        codename = None
        with open(os_release_file, 'r') as f:
            for line in f:
                match = re.match(r'^VERSION_CODENAME=(.+)$', line.strip())
                if match:
                    codename = match.group(1).strip('"')
                if debian_version is None:
                    match = re.match(r'^VERSION_ID=(.+)$', line.strip())
                    if match:
                        debian_version = match.group(1).strip('"').split('.')[0]

        return {
            'version': debian_version or 'unknown',
            'codename': codename or 'unknown',
        }

    def _build_impl(self):
        shutil.copytree(self.source_path, self.source_in_chroot, dirs_exist_ok=True, symlinks=True)
        if os.path.exists(os.path.join(self.dpkg_overlay_packages_path, 'Packages.gz')):
            self.run_in_chroot('apt update')

        self.setup_ccache()
        self.execute_pre_depends_commands()

        self.run_in_chroot(f'cd {self.package_source} && mk-build-deps --build-dep', 'Failed mk-build-deps')
        self.run_in_chroot(f'cd {self.package_source} && apt install -y ./*.deb', 'Failed install build deps')

        # Truenas package is special
        if self.name == 'truenas':
            debian_info = self._get_debian_info()
            debian_version = debian_info['version']
            codename = debian_info['codename']

            os.makedirs(os.path.join(self.package_source_with_chroot, 'data'))
            with open(os.path.join(self.package_source_with_chroot, 'data/manifest.json'), 'w') as f:
                f.write(json.dumps({
                    'buildtime': BUILD_TIME,
                    'train': get_truenas_train(),
                    'codename': get_release_code_name(),
                    'version': VERSION,
                }))
            os.makedirs(os.path.join(self.package_source_with_chroot, 'etc'), exist_ok=True)
            with open(os.path.join(self.package_source_with_chroot, 'etc/version'), 'w') as f:
                f.write(VERSION)
            os.makedirs(os.path.join(self.package_source_with_chroot, 'usr/lib'), exist_ok=True)
            with open(os.path.join(self.package_source_with_chroot, 'usr/lib/os-release.truenas'), 'w') as f:
                f.write(
                    '\n'.join([
                        f'PRETTY_NAME="{TRUENAS_VENDOR}/Debian {debian_version} ({codename})"',
                        f'NAME="{TRUENAS_VENDOR}/Debian"',
                        f'ID="{TRUENAS_VENDOR}"',
                        f'VERSION="{VERSION}"',
                        f'VERSION_ID="{VERSION}"',
                        f'VERSION_CODENAME="{codename}"',
                        'HOME_URL="https://harboros.ai/"',
                        'SUPPORT_URL="https://github.com/HarborNAS/community/discussions/categories/feedback"',
                        'BUG_REPORT_URL="https://github.com/HarborNAS/community/discussions/categories/bug"',
                        '',
                    ])
                )
            # /etc/issue.truenas - for local console login (includes \n \l terminal escapes)
            with open(os.path.join(self.package_source_with_chroot, 'etc/issue.truenas'), 'w') as f:
                f.write(f"{TRUENAS_VENDOR} based on Debian GNU/Linux {debian_version} \\n \\l\n")
            # /etc/issue.net.truenas - for network login (no terminal escapes)
            with open(os.path.join(self.package_source_with_chroot, 'etc/issue.net.truenas'), 'w') as f:
                f.write(f"{TRUENAS_VENDOR} based on Debian GNU/Linux {debian_version}\n")

        for prebuild_command in self.prebuildcmd:
            self.logger.debug('Running prebuildcmd: %r', prebuild_command)
            self.run_in_chroot(
                f'cd {self.package_source} && {prebuild_command}', 'Failed to execute prebuildcmd command'
            )

        # Make a programmatically generated version for this build
        generate_version_flags = ''
        if self.generate_version:
            generate_version_flags = f' -v {datetime.today().strftime("%Y%m%d%H%M%S")}~truenas+1 '

        debian_release = get_manifest()['debian_release']
        distribution = f'{debian_release}-truenas-unstable'
        self.run_in_chroot(
            f'cd {self.package_source} && dch -b -M {generate_version_flags}--force-distribution '
            f'--distribution {distribution} \'Tagged from truenas-build\'',
            'Failed dch changelog'
        )

        for command in self.build_command:
            self.logger.debug('Running build command: %r', command)
            self.run_in_chroot(
                f'cd {self.package_source} && {command}', f'Failed to build {self.name} package'
            )

        self.logger.debug('Copying finished packages')
        # Copy and record each built packages for cleanup later
        package_dir = os.path.dirname(self.package_source_with_chroot)
        built_packages = []
        for pkg in filter(lambda p: p.endswith(('.deb', '.udeb')), os.listdir(package_dir)):
            shutil.move(os.path.join(package_dir, pkg), os.path.join(PKG_DIR, pkg))
            built_packages.append(pkg)

        with open(self.pkglist_hash_file_path, 'w') as f:
            f.write('\n'.join(built_packages))

        with open(self.hash_path, 'w') as f:
            f.write(self.source_hash)

        self.delete_overlayfs()

    def execute_pre_depends_commands(self):
        for predep_entry in self.predepscmd:
            if isinstance(predep_entry, dict):
                predep_cmd = predep_entry['command']
                skip_cmd = False
                build_env = self._get_build_env()
                for env_var in predep_entry['env_checks']:
                    if build_env.get(env_var['key']) != env_var['value']:
                        self.logger.debug(
                            'Skipping %r predep command because %r does not match %r',
                            predep_cmd, env_var['key'], env_var['value']
                        )
                        skip_cmd = True
                        break
                if skip_cmd:
                    continue
            else:
                predep_cmd = predep_entry

            self.logger.debug('Running predepcmd: %r', predep_cmd)
            self.run_in_chroot(
                f'cd {self.package_source} && {predep_cmd}', 'Failed to execute predep command'
            )

        if not os.path.exists(os.path.join(self.package_source_with_chroot, 'debian/control')):
            raise CallError(
                f'Missing debian/control file for {self.name} in {self.package_source_with_chroot}'
            )

    @property
    def build_command(self):
        if self.buildcmd:
            return self.buildcmd
        else:
            build_env = f'DEB_BUILD_OPTIONS={self.deoptions} ' if self.deoptions else ''
            env_flags = [f'-e{k}' for k in self._get_chroot_env()]
            return [f'{build_env} debuild {" ".join(env_flags + self.deflags)}']

    @property
    def debug_command(self):
        return f'chroot {self.dpkg_overlay} /bin/bash'

    @property
    def deflags(self):
        return ['--no-lintian', f'-j{self.jobs if self.jobs else os.cpu_count()}', '-us', '-uc', '-b']

    @contextlib.contextmanager
    def build_dir(self):
        try:
            self.delete_overlayfs()
            self.setup_chroot_basedir()
            self.make_overlayfs()
            shutil.copytree(self.source_path, self.source_in_chroot, dirs_exist_ok=True, symlinks=True)
            yield
        finally:
            self.delete_overlayfs()
