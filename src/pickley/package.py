import os
import sys
import time
import zipfile

import virtualenv

from pickley import ImplementationMap, PingLock, PingLockException, short, system
from pickley.install import PexRunner, PipRunner
from pickley.pypi import latest_pypi_version, read_entry_points
from pickley.settings import JsonSerializable, SETTINGS
from pickley.uninstall import uninstall_existing


PACKAGERS = ImplementationMap(SETTINGS, "packager")
DELIVERERS = ImplementationMap(SETTINGS, "delivery")

GENERIC_WRAPPER = """
#!/bin/bash

# Wrapper generated by https://pypi.org/project/pickley/

if [[ -x {pickley} ]]; then
    nohup {pickley} auto-upgrade {name} &> /dev/null &
fi
if [[ -x {source} ]]; then
    exec {source} $*
else
    echo "{source} is not available anymore"
    echo ""
    echo "Please reinstall with:"
    echo "{pickley} install -f {name}"
    exit 1
fi
"""

# Specific wrapper for pickley itself (better handling bootstrap)
PICKLEY_WRAPPER = """
#!/bin/bash

# Wrapper generated by https://pypi.org/project/pickley/

if [[ -x {source} ]]; then
    if [[ $1 != "auto-upgrade" ]]; then
        nohup {source} auto-upgrade {name} &> /dev/null &
    fi
    exec {source} $*
else
    echo "{source} is not available anymore"
    echo ""
    echo "Please reinstall with:"
    url=`curl -s https://pypi.org/pypi/pickley/json | grep -Eo '"download_url":"([^"]+)"' | cut -d'"' -f4`
    echo curl -sLo {pickley} $url
    exit 1
fi
"""


class DeliveryMethod:
    """
    Various implementation of delivering the actual executables
    """

    def __init__(self, package_name):
        self.package_name = package_name

    @classmethod
    def class_implementation_name(cls):
        """
        :return str: Identifier for this delivery type
        """
        return cls.__name__.replace("Delivery", "").lower()

    @property
    def implementation_name(self):
        """
        :return str: Identifier for this delivery type
        """
        return self.__class__.class_implementation_name()

    def install(self, target, source):
        """
        :param str target: Full path of executable to deliver (<base>/<entry_point>)
        :param str source: Path to original executable being delivered (.pickley/<package>/...)
        """
        system.delete_file(target)
        if system.DRYRUN:
            system.debug("Would %s %s (source: %s)", self.implementation_name, short(target), short(source))
            return

        if not os.path.exists(source):
            system.abort("Can't %s, source %s does not exist", self.implementation_name, short(source))

        try:
            system.debug("Delivery: %s %s -> %s", self.implementation_name, short(target), short(source))
            self._install(target, source)

        except Exception as e:
            system.abort("Failed %s %s: %s", self.implementation_name, short(target), e)

    def _install(self, target, source):
        """
        :param str target: Full path of executable to deliver (<base>/<entry_point>)
        :param str source: Path to original executable being delivered (.pickley/<package>/...)
        """


@DELIVERERS.register
class DeliverySymlink(DeliveryMethod):
    """
    Deliver via symlink
    """

    def _install(self, target, source):
        if os.path.isabs(source) and os.path.isabs(target):
            parent = system.parent_folder(target)
            if system.parent_folder(source).startswith(parent):
                # Use relative path if source is under target
                source = os.path.relpath(source, parent)
        os.symlink(source, target)


@DELIVERERS.register
class DeliveryWrap(DeliveryMethod):
    """
    Deliver via a small wrap that ensures target executable is up-to-date
    """

    def _install(self, target, source):
        # Touch the .ping file since this is a fresh install (no need to check for upgrades right away)
        ping = PingLock(SETTINGS.meta.full_path(self.package_name), seconds=SETTINGS.version_check_delay)
        ping.touch()

        if self.package_name == system.PICKLEY:
            # Important: call pickley auto-upgrade from souce, and not wrapper in order to avoid infinite recursion
            wrapper = PICKLEY_WRAPPER
        else:
            wrapper = GENERIC_WRAPPER

        contents = wrapper.lstrip().format(
            name=system.quoted(self.package_name),
            pickley=system.quoted(SETTINGS.base.full_path(system.PICKLEY)),
            source=system.quoted(source),
        )
        system.write_contents(target, contents)
        system.make_executable(target)


@DELIVERERS.register
class DeliveryCopy(DeliveryMethod):
    """
    Deliver by copy
    """

    def _install(self, target, source):
        system.copy_file(source, target)


class VersionMeta(JsonSerializable):
    """
    Version meta on a given package
    """

    _problem = None                 # type: str # Detected problem, if any
    _name = None                    # type: str # Associated pypi package name
    channel = ""                    # type: str # Channel (stable, latest, ...) via which this version was determined
    packager = ""                   # type: str # Packager used
    delivery = ""                   # type: str # Delivery method used
    python = ""                     # type: str # Python interpreter used
    source = ""                     # type: str # Description of where definition came from
    timestamp = None                # type: float # Epoch when version was determined (useful to cache "expensive" calls to pypi)
    version = ""                    # type: str # Effective version

    def __init__(self, name, suffix=None):
        """
        :param str name: Associated pypi package name
        :param str|None suffix: Optional suffix where to store this object
        """
        self._name = name
        if suffix:
            self._path = SETTINGS.meta.full_path(self.name, ".%s.json" % suffix)

    def __repr__(self):
        return self.representation()

    def representation(self, verbose=False, note=None):
        """
        :param bool verbose: If True, show more extensive info
        :return str: Human readable representation
        """
        if self._problem:
            lead = "%s: %s" % (self.name, self._problem)
        elif self.version:
            lead = "%s %s" % (self.name, self.version)
        else:
            lead = "%s: no version" % (self.name)
        notice = ""
        if verbose:
            notice = []
            if self.packager or self.delivery:
                info = "as"
                if self.packager:
                    info = "%s %s" % (info, self.packager)
                if self.delivery:
                    info = "%s %s" % (info, self.delivery)
                notice.append(info)
            if self.channel:
                notice.append("channel: %s" % self.channel)
            if self.source and self.source != SETTINGS.index:
                notice.append("source: %s" % self.source)
            if notice:
                notice = " (%s)" % ", ".join(notice)
            else:
                notice = ""
        if note:
            notice = " %s%s" % (note, notice)
        return "%s%s" % (lead, notice)

    @property
    def name(self):
        """
        :return str: Associated pypi package name
        """
        return self._name

    @property
    def problem(self):
        """
        :return str|None: Problem description, if any
        """
        return self._problem

    @property
    def valid(self):
        """
        :return bool: Was version determined successfully?
        """
        return bool(self.version) and not self._problem

    @property
    def file_exists(self):
        """
        :return bool: True if corresponding json file exists
        """
        return self._path and os.path.exists(self._path)

    def equivalent(self, other):
        """
        :param VersionMeta other: VersionMeta to compare to
        :return bool: True if 'self' is equivalent to 'other'
        """
        if other is None:
            return False
        if self.version != other.version:
            return False
        if self.packager != other.packager:
            return False
        return True

    def set_version(self, version, source, channel=""):
        """
        :param str version: Effective version
        :param str source: Description of where version determination came from
        :param str channel: Channel (stable, latest, ...) via which this version was determined
        """
        self.version = version
        self.source = source
        self.channel = channel
        self.timestamp = time.time()

    def set(self, *others):
        for other in others:
            if isinstance(other, VersionMeta):
                self._problem = other._problem
                self.channel = other.channel
                if other.packager:
                    self.packager = other.packager
                if other.delivery:
                    self.delivery = other.delivery
                if other.python:
                    self.python = other.python
                self.source = other.source
                self.timestamp = other.timestamp
                self.version = other.version
            elif isinstance(other, Packager):
                self.packager = other.implementation_name
                delivery = DELIVERERS.resolved(other.name)
                if isinstance(delivery, DeliveryMethod):
                    self.delivery = delivery.implementation_name
                self.python = SETTINGS.resolved_value("python", other.name)

    def invalidate(self, problem):
        """
        :param str problem: Description of problem
        """
        self._problem = problem
        self.version = ""

    @property
    def still_valid(self):
        """
        :return bool: Is this version determination still valid? (based on timestamp)
        """
        if not self.valid or not self.timestamp:
            return self.valid
        try:
            return (time.time() - self.timestamp) < SETTINGS.version_check_delay
        except Exception:
            return False


class Packager(object):
    """
    Interface of a packager
    """

    def __init__(self, name):
        """
        :param str name: Name of pypi package
        """
        self.name = name
        self._entry_points = None
        self.current = VersionMeta(self.name, "current")
        self.latest = VersionMeta(self.name, system.DEFAULT_CHANNEL)
        self.desired = VersionMeta(self.name)
        self.dist_folder = SETTINGS.meta.full_path(self.name, ".work")
        self.build_folder = os.path.join(self.dist_folder, "build")
        self.source_folder = None

    def __repr__(self):
        return "%s %s" % (self.implementation_name, self.name)

    @classmethod
    def class_implementation_name(cls):
        """
        :return str: Identifier for this packager type
        """
        return cls.__name__.replace("Packager", "").lower()

    @property
    def implementation_name(self):
        """
        :return str: Identifier for this packager type
        """
        return self.__class__.class_implementation_name()

    @property
    def entry_points_path(self):
        return SETTINGS.meta.full_path(self.name, ".entry-points.json")

    @property
    def entry_points(self):
        """
        :return list|None: Determined entry points from produced wheel, if available
        """
        if self._entry_points is None:
            self._entry_points = JsonSerializable.get_json(self.entry_points_path)
            if self._entry_points is None:
                self._entry_points = [self.name] if system.DRYRUN else []
        return self._entry_points

    def refresh_entry_points(self, folder, version):
        """
        :param str folder: Folder where to look for entry points
        :param str version: Version of package
        """
        if system.DRYRUN:
            return
        self._entry_points = self.get_entry_points(folder, version)
        if not self._entry_points:
            system.abort("'%s' is not a CLI, it has no console_scripts entry points", self.name)
        JsonSerializable.save_json(self._entry_points, self.entry_points_path)

    def get_entry_points(self, folder, version):
        """
        :param str folder: Folder where to look for entry points
        :param str version: Version of package
        :return list|None: Determine entry points for pypi package with 'self.name'
        """
        if not os.path.isdir(self.build_folder):
            return None

        prefix = "%s-%s-" % (self.name, version)
        for fname in os.listdir(self.build_folder):
            if fname.startswith(prefix) and fname.endswith(".whl"):
                wheel_path = os.path.join(self.build_folder, fname)
                try:
                    with zipfile.ZipFile(wheel_path, "r") as wheel:
                        for fname in wheel.namelist():
                            if os.path.basename(fname) == "entry_points.txt":
                                with wheel.open(fname) as fh:
                                    return read_entry_points(fh)

                except Exception as e:
                    system.error("Can't read wheel %s: %s", wheel_path, e, exc_info=e)

        return None

    def refresh_current(self):
        """Refresh self.current"""
        self.current.load()
        if not self.current.valid:
            self.current.invalidate("is not installed")

    def refresh_latest(self):
        """Refresh self.latest"""
        self.latest.load()
        if self.latest.still_valid:
            return

        version = latest_pypi_version(SETTINGS.index, self.name)
        self.latest.set_version(version, SETTINGS.index or "pypi", channel=system.DEFAULT_CHANNEL)
        if version:
            self.latest.save()

        else:
            self.latest.invalidate("can't determine latest version")

    def refresh_desired(self):
        """Refresh self.desired"""
        channel = SETTINGS.resolved_definition("channel", package_name=self.name)
        v = SETTINGS.get_definition("channel.%s.%s" % (channel.value, self.name))
        if v and v.value:
            self.desired.set_version(v.value, str(v), channel=channel.value)
            self.desired.set(self)
            return

        if channel.value == system.DEFAULT_CHANNEL:
            self.refresh_latest()
            self.desired.set(self, self.latest)
            return

        self.desired.invalidate("can't determine %s version" % channel.value)

    def pip_wheel(self, version):
        """
        Run pip wheel

        :param str version: Version to use
        :return str: None if successful, error message otherwise
        """
        pip = PipRunner(self.build_folder)
        return pip.wheel(self.source_folder if self.source_folder else "%s==%s" % (self.name, version))

    def package(self, version=None):
        """
        :param str|None version: If provided, append version as suffix to produced pex
        :return list: List of produced packages (files), if successful
        """
        if not version and not self.source_folder:
            system.abort("Need either source_folder or version in order to package")

        if not version:
            setup_py = os.path.join(self.source_folder, "setup.py")
            if not os.path.isfile(setup_py):
                system.abort("No setup.py in %s", short(self.source_folder))
            version = system.run_program(sys.executable, setup_py, "--version", fatal=False)
            if not version:
                system.abort("Could not determine version from %s", short(setup_py))

        error = self.pip_wheel(version)
        if error:
            system.abort("pip wheel failed: %s", error)

        self.refresh_entry_points(self.build_folder, version)
        system.ensure_folder(self.dist_folder, folder=True)
        template = "{name}" if self.source_folder else "{name}-{version}"
        return self.effective_package(template, version)

    def effective_package(self, template, version=None):
        """
        :param str|None version: If provided, append version as suffix to produced pex
        :param str template: Template describing how to name delivered files, example: {meta}/{name}-{version}
        :return list: List of produced packages (files), if successful
        """

    def install(self, force=False):
        """
        :param bool force: If True, re-install even if package is already installed
        """
        try:
            self.internal_install(force=force)

        except PingLockException as e:
            system.error("%s is currently being installed by another process" % self.name)
            system.abort("If that is incorrect, please delete %s", short(e.ping_path))

    def internal_install(self, force=False, bootstrap=False):
        """
        :param bool force: If True, re-install even if package is already installed
        :param bool bootstrap: Bootstrap mode
        """
        with PingLock(self.dist_folder, seconds=SETTINGS.install_timeout):
            intent = "bootstrap" if bootstrap else "install"
            self.refresh_desired()
            if not self.desired.valid:
                system.abort("Can't %s %s: %s", intent, self.name, self.desired.problem)

            self.refresh_current()
            if not force and self.current.equivalent(self.desired):
                if not bootstrap:
                    system.info(self.desired.representation(verbose=True, note="is already installed"))
                return

            if bootstrap:
                system.debug("Bootstrapping %s with %s", system.PICKLEY, self.implementation_name)

            prev_entry_points = self.entry_points or []
            installed = self.effective_install(self.desired.version)

            target = SETTINGS.meta.full_path(self.name)
            if os.path.isdir(target):
                for name in os.listdir(target):
                    if not name.startswith("."):
                        fpath = os.path.join(target, name)
                        if fpath not in installed:
                            system.delete_file(fpath)

            new_entry_points = self.entry_points
            for name in prev_entry_points:
                if name not in new_entry_points:
                    # Entry point was removed by package
                    system.delete_file(SETTINGS.base.full_path(name))

            self.current.set(self, self.desired)
            self.current.save()

            msg = "Would %s" % intent if system.DRYRUN else "%sed" % (intent.title())
            system.info("%s %s", msg, self.desired.representation(verbose=True))

    def effective_install(self, version):
        """
        :param str version: Effective version to install
        :return list: Full path to installed files/folders
        """

    def perform_delivery(self, version, template):
        """
        :param str version: Version being delivered
        :param str template: Template describing how to name delivered files, example: {meta}/{name}-{version}
        """
        deliverer = DELIVERERS.resolved(self.name)
        for name in self.entry_points:
            target = SETTINGS.base.full_path(name)
            if self.name != system.PICKLEY and not self.current.file_exists:
                uninstall_existing(target)
            path = template.format(meta=SETTINGS.meta.full_path(self.name), name=name, version=version)
            deliverer.install(target, path)


@PACKAGERS.register
class PexPackager(Packager):
    """
    Package/install via pex (https://pypi.org/project/pex/)
    """

    def pex_build(self, name, version, dest):
        """
        Run pex build

        :param str name: Name of entry point
        :param str version: Version to use
        :param str dest: Path to file where to produce pex
        :return str: None if successful, error message otherwise
        """
        pex = PexRunner(self.build_folder)
        return pex.build(name, self.name, version, dest)

    def effective_package(self, template, version=None):
        """
        :param str|None version: If provided, append version as suffix to produced pex
        :param str template: Template describing how to name delivered files, example: {meta}/{name}-{version}
        :return list: List of produced packages (files), if successful
        """
        result = []
        for name in self.entry_points:
            dest = template.format(name=name, version=version)
            dest = os.path.join(self.dist_folder, dest)

            error = self.pex_build(name, version, dest)
            if error:
                system.abort("pex command failed: %s", error)
            result.append(dest)

        return result

    def effective_install(self, version):
        """
        :param str version: Effective version to install
        :return list: Full path to installed files/folders
        """
        result = []
        packaged = self.package(version=version)
        for path in packaged:
            name = os.path.basename(path)
            target = SETTINGS.meta.full_path(self.name, name)
            system.move_file(path, target)
            result.append(target)
        self.perform_delivery(version, "{meta}/{name}-{version}")
        return result


@PACKAGERS.register
class VenvPackager(Packager):
    """
    Install via virtualenv (https://pypi.org/project/virtualenv/)
    """

    def virtualenv_path(self):
        venv = virtualenv.__file__
        if venv and venv.endswith(".pyc"):
            venv = venv[:-1]
        return venv

    def effective_package(self, template, version=None):
        """
        :param str|None version: If provided, append version as suffix to produced pex
        :param str template: Template describing how to name delivered files, example: {meta}/{name}-{version}
        :return list: List of produced packages (files), if successful
        """
        venv = self.virtualenv_path()
        if not venv:
            system.abort("Can't determine path to virtualenv.py")

        working_folder = os.path.join(self.dist_folder, template.format(name=self.name, version=version))
        python = SETTINGS.resolved_value("python", package_name=self.name)
        if python == system.PYTHON:
            python = None
        pip = os.path.join(working_folder, "bin", "pip")
        system.run_program(system.PYTHON, venv, "-p", python, working_folder)
        system.run_program(pip, "install", "-i", SETTINGS.index, "-f", self.build_folder, "%s==%s" % (self.name, version))

        return [working_folder]

    def effective_install(self, version):
        """
        :param str version: Effective version to install
        :return list: Full path to installed files/folders
        """
        result = []
        packaged = self.package(version=version)
        for path in packaged:
            target = SETTINGS.meta.full_path(self.name, os.path.basename(path))
            system.move_venv(path, target)
            result.append(target)
            self.perform_delivery(version, os.path.join(target, "bin", "{name}"))
        return result
