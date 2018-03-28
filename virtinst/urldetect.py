#
# Copyright 2006-2007, 2013 Red Hat, Inc.
# Daniel P. Berrange <berrange@redhat.com>
#
# This work is licensed under the GNU GPLv2.
# See the COPYING file in the top-level directory.

import configparser
import logging
import os
import re

from .osdict import OSDB


###############################################
# Helpers for detecting distro from given URL #
###############################################

def _grabTreeinfo(fetcher):
    """
    See if the URL has treeinfo, and if so return it as a ConfigParser
    object.
    """
    try:
        tmptreeinfo = fetcher.acquireFile(".treeinfo")
    except ValueError:
        return None

    try:
        treeinfo = configparser.SafeConfigParser()
        treeinfo.read(tmptreeinfo)
    finally:
        os.unlink(tmptreeinfo)

    try:
        treeinfo.get("general", "family")
    except configparser.NoSectionError:
        logging.debug("Did not find 'family' section in treeinfo")
        return None

    logging.debug("treeinfo family=%s", treeinfo.get("general", "family"))
    return treeinfo


def _parseSUSEContent(cbuf):
    distribution = None
    distro_version = None
    distro_summary = None
    distro_distro = None
    distro_arch = None

    # As of 2018 all latest distros match only DISTRO and REPOID below
    for line in cbuf.splitlines()[1:]:
        if line.startswith("LABEL "):
            # opensuse 10.3: LABEL openSUSE 10.3
            # opensuse 11.4: LABEL openSUSE 11.4
            # opensuse 12.3: LABEL openSUSE
            # sles11sp4 DVD: LABEL SUSE Linux Enterprise Server 11 SP4
            distribution = line.split(' ', 1)
        elif line.startswith("DISTRO "):
            # DISTRO cpe:/o:opensuse:opensuse:13.2,openSUSE
            # DISTRO cpe:/o:suse:sled:12:sp3,SUSE Linux Enterprise Desktop 12 SP3
            distro_distro = line.rsplit(',', 1)
        elif line.startswith("VERSION "):
            # opensuse 10.3: VERSION 10.3
            # opensuse 12.3: VERSION 12.3
            distro_version = line.split(' ', 1)
            if len(distro_version) > 1:
                d_version = distro_version[1].split('-', 1)
                if len(d_version) > 1:
                    distro_version[1] = d_version[0]
        elif line.startswith("SUMMARY "):
            distro_summary = line.split(' ', 1)
        elif line.startswith("BASEARCHS "):
            # opensuse 11.4: BASEARCHS i586 x86_64
            # opensuse 12.3: BASEARCHS i586 x86_64
            distro_arch = line.split(' ', 1)
        elif line.startswith("DEFAULTBASE "):
            # opensuse 10.3: DEFAULTBASE i586
            distro_arch = line.split(' ', 1)
        elif line.startswith("REPOID "):
            # REPOID obsproduct://build.suse.de/SUSE:SLE-11-SP4:GA/SUSE_SLES/11.4/DVD/x86_64
            # REPOID obsproduct://build.suse.de/SUSE:SLE-12-SP3:GA/SLES/12.3/DVD/aarch64
            distro_arch = line.rsplit('/', 1)
        if distribution and distro_version and distro_arch:
            break

    if not distribution:
        if distro_summary:
            distribution = distro_summary
        elif distro_distro:
            distribution = distro_distro

    tree_arch = None
    if distro_arch:
        tree_arch = distro_arch[1].strip()
        # Fix for 13.2 official oss repo
        if tree_arch.find("i586-x86_64") != -1:
            tree_arch = "x86_64"
    else:
        if cbuf.find("x86_64") != -1:
            tree_arch = "x86_64"
        elif cbuf.find("i586") != -1:
            tree_arch = "i586"
        elif cbuf.find("s390x") != -1:
            tree_arch = "s390x"

    return distribution, distro_version, tree_arch


def _distroFromSUSEContent(fetcher, arch, vmtype):
    try:
        cbuf = fetcher.acquireFileContent("content")
    except ValueError:
        return None

    distribution, distro_version, tree_arch = _parseSUSEContent(cbuf)
    logging.debug("SUSE content file found distribution=%s distro_version=%s "
        "tree_arch=%s", distribution, distro_version, tree_arch)

    def _parse_sle_distribution(d):
        sle_version = d[1].strip().rsplit(' ')[4]
        if len(d[1].strip().rsplit(' ')) > 5:
            sle_version = sle_version + '.' + d[1].strip().rsplit(' ')[5][2]
        return ['VERSION', sle_version]

    dclass = GenericDistro
    if distribution:
        if re.match(".*SUSE Linux Enterprise Server*", distribution[1]) or \
                re.match(".*SUSE SLES*", distribution[1]):
            dclass = SLESDistro
            if distro_version is None:
                distro_version = _parse_sle_distribution(distribution)
        elif re.match(".*SUSE Linux Enterprise Desktop*", distribution[1]):
            dclass = SLEDDistro
            if distro_version is None:
                distro_version = _parse_sle_distribution(distribution)
        elif re.match(".*openSUSE.*", distribution[1]):
            dclass = OpensuseDistro
            if distro_version is None:
                distro_version = ['VERSION', distribution[0].strip().rsplit(':')[4]]

    if distro_version is None:
        logging.debug("No specified SUSE version detected")
        return None

    ob = dclass(fetcher, tree_arch or arch, vmtype)
    if dclass != GenericDistro:
        ob.version_from_content = distro_version

    # Explictly call this, so we populate os_type/variant info
    ob.isValidStore()

    return ob


def getDistroStore(guest, fetcher):
    stores = []
    logging.debug("Finding distro store for location=%s", fetcher.location)

    arch = guest.os.arch
    _type = guest.os.os_type
    urldistro = OSDB.lookup_os(guest.os_variant).urldistro

    treeinfo = _grabTreeinfo(fetcher)
    if not treeinfo:
        dist = _distroFromSUSEContent(fetcher, arch, _type)
        if dist:
            return dist

    stores = _allstores[:]

    # If user manually specified an os_distro, bump it's URL class
    # to the top of the list
    if urldistro:
        logging.debug("variant=%s has distro=%s, looking for matching "
                      "distro store to prioritize",
                      guest.os_variant, urldistro)
        found_store = None
        for store in stores:
            if store.urldistro == urldistro:
                found_store = store

        if found_store:
            logging.debug("Prioritizing distro store=%s", found_store)
            stores.remove(found_store)
            stores.insert(0, found_store)
        else:
            logging.debug("No matching store found, not prioritizing anything")

    if treeinfo:
        stores.sort(key=lambda x: not x.uses_treeinfo)

    for sclass in stores:
        store = sclass(fetcher, arch, _type)
        store.treeinfo = treeinfo
        if store.isValidStore():
            logging.debug("Detected distro name=%s osvariant=%s",
                          store.name, store.os_variant)
            return store

    # No distro was detected. See if the URL even resolves, and if not
    # give the user a hint that maybe they mistyped. This won't always
    # be true since some webservers don't allow directory listing.
    # http://www.redhat.com/archives/virt-tools-list/2014-December/msg00048.html
    extramsg = ""
    if not fetcher.hasFile(""):
        extramsg = (": " +
            _("The URL could not be accessed, maybe you mistyped?"))

    raise ValueError(
        _("Could not find an installable distribution at '%s'%s\n\n"
          "The location must be the root directory of an install tree.\n"
          "See virt-install man page for various distro examples." %
          (fetcher.location, extramsg)))


##################
# Distro classes #
##################

class Distro(object):
    """
    An image store is a base class for retrieving either a bootable
    ISO image, or a kernel+initrd  pair for a particular OS distribution
    """
    name = None
    urldistro = None
    uses_treeinfo = False

    # osdict variant value
    os_variant = None

    _boot_iso_paths = []
    _hvm_kernel_paths = []
    _xen_kernel_paths = []
    version_from_content = []

    def __init__(self, fetcher, arch, vmtype):
        self.fetcher = fetcher
        self.type = vmtype
        self.arch = arch

        self.uri = fetcher.location

        # This is set externally
        self.treeinfo = None

    def isValidStore(self):
        """Determine if uri points to a tree of the store's distro"""
        raise NotImplementedError

    def acquireKernel(self, guest):
        kernelpath = None
        initrdpath = None
        if self.treeinfo:
            try:
                kernelpath = self._getTreeinfoMedia("kernel")
                initrdpath = self._getTreeinfoMedia("initrd")
            except configparser.NoSectionError:
                pass

        if not kernelpath or not initrdpath:
            # fall back to old code
            if self.type is None or self.type == "hvm":
                paths = self._hvm_kernel_paths
            else:
                paths = self._xen_kernel_paths

            for kpath, ipath in paths:
                if self.fetcher.hasFile(kpath) and self.fetcher.hasFile(ipath):
                    kernelpath = kpath
                    initrdpath = ipath

        if not kernelpath or not initrdpath:
            raise RuntimeError(_("Couldn't find %(type)s kernel for "
                                 "%(distro)s tree.") %
                                 {"distro": self.name, "type": self.type})

        return self._kernelFetchHelper(guest, kernelpath, initrdpath)

    def acquireBootDisk(self, guest):
        ignore = guest

        if self.treeinfo:
            return self.fetcher.acquireFile(self._getTreeinfoMedia("boot.iso"))

        for path in self._boot_iso_paths:
            if self.fetcher.hasFile(path):
                return self.fetcher.acquireFile(path)
        raise RuntimeError(_("Could not find boot.iso in %s tree." %
                           self.name))

    def _check_osvariant_valid(self, os_variant):
        return OSDB.lookup_os(os_variant) is not None

    def get_osdict_info(self):
        """
        Return (distro, variant) tuple, checking to make sure they are valid
        osdict entries
        """
        if not self.os_variant:
            return None

        if not self._check_osvariant_valid(self.os_variant):
            logging.debug("%s set os_variant to %s, which is not in osdict.",
                          self, self.os_variant)
            return None

        return self.os_variant

    def _get_method_arg(self):
        return "method"

    def _getTreeinfoMedia(self, mediaName):
        if self.type == "xen":
            t = "xen"
        else:
            t = self.treeinfo.get("general", "arch")

        return self.treeinfo.get("images-%s" % t, mediaName)

    def _fetchAndMatchRegex(self, filename, regex):
        # Fetch 'filename' and return True/False if it matches the regex
        try:
            content = self.fetcher.acquireFileContent(filename)
        except ValueError:
            return False

        for line in content.splitlines():
            if re.match(regex, line):
                return True

        return False

    def _kernelFetchHelper(self, guest, kernelpath, initrdpath):
        # Simple helper for fetching kernel + initrd and performing
        # cleanup if necessary
        ignore = guest
        kernel = self.fetcher.acquireFile(kernelpath)
        args = ''

        if not self.fetcher.location.startswith("/"):
            args += "%s=%s" % (self._get_method_arg(), self.fetcher.location)

        try:
            initrd = self.fetcher.acquireFile(initrdpath)
            return kernel, initrd, args
        except Exception:
            os.unlink(kernel)
            raise


class GenericDistro(Distro):
    """
    Generic distro store. Check well known paths for kernel locations
    as a last resort if we can't recognize any actual distro
    """
    name = "Generic"
    uses_treeinfo = True

    _xen_paths = [("images/xen/vmlinuz",
                    "images/xen/initrd.img"),           # Fedora
                  ]
    _hvm_paths = [("images/pxeboot/vmlinuz",
                    "images/pxeboot/initrd.img"),       # Fedora
                  ("ppc/ppc64/vmlinuz",
                    "ppc/ppc64/initrd.img"),            # CenOS 7 ppc64le
                  ]
    _iso_paths = ["images/boot.iso",                   # RH/Fedora
                   "boot/boot.iso",                     # Suse
                   "current/images/netboot/mini.iso",   # Debian
                   "install/images/boot.iso",           # Mandriva
                  ]

    # Holds values to use when actually pulling down media
    _valid_kernel_path = None
    _valid_iso_path = None

    def isValidStore(self):
        if self.treeinfo:
            # Use treeinfo to pull down media paths
            if self.type == "xen":
                typ = "xen"
            else:
                typ = self.treeinfo.get("general", "arch")

            kernelSection = "images-%s" % typ
            isoSection = "images-%s" % self.treeinfo.get("general", "arch")

            if self.treeinfo.has_section(kernelSection):
                try:
                    self._valid_kernel_path = (
                        self._getTreeinfoMedia("kernel"),
                        self._getTreeinfoMedia("initrd"))
                except (configparser.NoSectionError,
                        configparser.NoOptionError) as e:
                    logging.debug(e)

            if self.treeinfo.has_section(isoSection):
                try:
                    self._valid_iso_path = self.treeinfo.get(isoSection,
                                                             "boot.iso")
                except configparser.NoOptionError as e:
                    logging.debug(e)

        if self.type == "xen":
            kern_list = self._xen_paths
        else:
            kern_list = self._hvm_paths

        # If validated media paths weren't found (no treeinfo), check against
        # list of media location paths.
        for kern, init in kern_list:
            if (self._valid_kernel_path is None and
                self.fetcher.hasFile(kern) and
                self.fetcher.hasFile(init)):
                self._valid_kernel_path = (kern, init)
                break

        for iso in self._iso_paths:
            if (self._valid_iso_path is None and
                self.fetcher.hasFile(iso)):
                self._valid_iso_path = iso
                break

        if self._valid_kernel_path or self._valid_iso_path:
            return True
        return False

    def acquireKernel(self, guest):
        if self._valid_kernel_path is None:
            raise ValueError(_("Could not find a kernel path for virt type "
                               "'%s'" % self.type))

        return self._kernelFetchHelper(guest,
                                       self._valid_kernel_path[0],
                                       self._valid_kernel_path[1])

    def acquireBootDisk(self, guest):
        if self._valid_iso_path is None:
            raise ValueError(_("Could not find a boot iso path for this tree."))

        return self.fetcher.acquireFile(self._valid_iso_path)


class RedHatDistro(Distro):
    """
    Base image store for any Red Hat related distros which have
    a common layout
    """
    uses_treeinfo = True
    _version_number = None

    _boot_iso_paths   = ["images/boot.iso"]
    _hvm_kernel_paths = [("images/pxeboot/vmlinuz",
                           "images/pxeboot/initrd.img")]
    _xen_kernel_paths = [("images/xen/vmlinuz",
                           "images/xen/initrd.img")]

    def isValidStore(self):
        raise NotImplementedError()

    def _get_method_arg(self):
        if (self._version_number is not None and
            ((self.urldistro == "rhel" and self._version_number >= 7) or
             (self.urldistro == "fedora" and self._version_number >= 19))):
            return "inst.repo"
        return "method"


# Fedora distro check
class FedoraDistro(RedHatDistro):
    name = "Fedora"
    urldistro = "fedora"

    def isValidStore(self):
        if not self.treeinfo:
            return self.fetcher.hasFile("Fedora")

        if not re.match(".*Fedora.*", self.treeinfo.get("general", "family")):
            return False

        ver = self.treeinfo.get("general", "version")
        if not ver:
            logging.debug("No version found in .treeinfo")
            return False
        logging.debug("Found treeinfo version=%s", ver)

        latest_variant = OSDB.latest_fedora_version()
        if re.match("fedora[0-9]+", latest_variant):
            latest_vernum = int(latest_variant[6:])
        else:
            logging.debug("Failed to parse version number from latest "
                "fedora variant=%s. Using safe default 22", latest_variant)
            latest_vernum = 22

        # rawhide trees changed to use version=Rawhide in Apr 2016
        if ver in ["development", "rawhide", "Rawhide"]:
            self._version_number = latest_vernum
            self.os_variant = latest_variant
            return True

        # Dev versions can be like '23_Alpha'
        if "_" in ver:
            ver = ver.split("_")[0]

        # Typical versions are like 'fedora-23'
        vernum = str(ver).split("-")[0]
        if vernum.isdigit():
            vernum = int(vernum)
        else:
            logging.debug("Failed to parse version number from treeinfo "
                "version=%s, using vernum=latest=%s", ver, latest_vernum)
            vernum = latest_vernum

        if vernum > latest_vernum:
            self.os_variant = latest_variant
        else:
            self.os_variant = "fedora" + str(vernum)

        self._version_number = vernum
        return True


# Red Hat Enterprise Linux distro check
class RHELDistro(RedHatDistro):
    name = "Red Hat Enterprise Linux"
    urldistro = "rhel"

    def isValidStore(self):
        if self.treeinfo:
            # Matches:
            #   Red Hat Enterprise Linux
            #   RHEL Atomic Host
            m = re.match(".*(Red Hat Enterprise Linux|RHEL).*",
                         self.treeinfo.get("general", "family"))
            ret = (m is not None)

            if ret:
                self._variantFromVersion()
            return ret

        if (self.fetcher.hasFile("Server") or
            self.fetcher.hasFile("Client")):
            self.os_variant = "rhel5"
            return True
        return self.fetcher.hasFile("RedHat")


    ################################
    # osdict autodetection helpers #
    ################################

    def _parseTreeinfoVersion(self, verstr):
        def _safeint(c):
            try:
                val = int(c)
            except Exception:
                val = 0
            return val

        version = _safeint(verstr[0])
        update = 0

        # RHEL has version=5.4, scientific linux=54
        updinfo = verstr.split(".")
        if len(updinfo) > 1:
            update = _safeint(updinfo[1])
        elif len(verstr) > 1:
            update = _safeint(verstr[1])

        return version, update

    def _variantFromVersion(self):
        ver = self.treeinfo.get("general", "version")
        name = None
        if self.treeinfo.has_option("general", "name"):
            name = self.treeinfo.get("general", "name")
        if not ver:
            return

        if name and name.startswith("Red Hat Enterprise Linux Server for ARM"):
            # Kind of a hack, but good enough for the time being
            version = 7
            update = 0
        else:
            version, update = self._parseTreeinfoVersion(ver)

        self._version_number = version
        self._setRHELVariant(version, update)

    def _setRHELVariant(self, version, update):
        base = "rhel" + str(version)
        if update < 0:
            update = 0

        ret = None
        while update >= 0:
            tryvar = base + ".%s" % update
            if not self._check_osvariant_valid(tryvar):
                update -= 1
                continue

            ret = tryvar
            break

        if not ret:
            # Try plain rhel5, rhel6, whatev
            if self._check_osvariant_valid(base):
                ret = base

        if ret:
            self.os_variant = ret


# CentOS distro check
class CentOSDistro(RHELDistro):
    name = "CentOS"
    urldistro = "centos"

    def isValidStore(self):
        if not self.treeinfo:
            return self.fetcher.hasFile("CentOS")

        m = re.match(".*CentOS.*", self.treeinfo.get("general", "family"))
        ret = (m is not None)
        if ret:
            self._variantFromVersion()
            if self.os_variant:
                new_variant = self.os_variant.replace("rhel", "centos")
                if self._check_osvariant_valid(new_variant):
                    self.os_variant = new_variant
        return ret


# Scientific Linux distro check
class SLDistro(RHELDistro):
    name = "Scientific Linux"
    urldistro = None

    _boot_iso_paths = RHELDistro._boot_iso_paths + ["images/SL/boot.iso"]
    _hvm_kernel_paths = RHELDistro._hvm_kernel_paths + [
        ("images/SL/pxeboot/vmlinuz", "images/SL/pxeboot/initrd.img")]

    def isValidStore(self):
        if self.treeinfo:
            m = re.match(".*Scientific.*",
                         self.treeinfo.get("general", "family"))
            ret = (m is not None)

            if ret:
                self._variantFromVersion()
            return ret

        return self.fetcher.hasFile("SL")


class SuseDistro(Distro):
    name = "SUSE"

    _boot_iso_paths   = ["boot/boot.iso"]

    def __init__(self, *args, **kwargs):
        Distro.__init__(self, *args, **kwargs)
        if re.match(r'i[4-9]86', self.arch):
            self.arch = 'i386'

        oldkern = "linux"
        oldinit = "initrd"
        if self.arch == "x86_64":
            oldkern += "64"
            oldinit += "64"

        if self.arch == "s390x":
            self._hvm_kernel_paths = [("boot/%s/linux" % self.arch,
                                       "boot/%s/initrd" % self.arch)]
            # No Xen on s390x
            self._xen_kernel_paths = []
        else:
            # Tested with Opensuse >= 10.2, 11, and sles 10
            self._hvm_kernel_paths = [("boot/%s/loader/linux" % self.arch,
                                        "boot/%s/loader/initrd" % self.arch)]
            # Tested with Opensuse 10.0
            self._hvm_kernel_paths.append(("boot/loader/%s" % oldkern,
                                           "boot/loader/%s" % oldinit))
            # Tested with SLES 12 for ppc64le
            self._hvm_kernel_paths.append(("boot/%s/linux" % self.arch,
                                           "boot/%s/initrd" % self.arch))

            # Matches Opensuse > 10.2 and sles 10
            self._xen_kernel_paths = [("boot/%s/vmlinuz-xen" % self.arch,
                                        "boot/%s/initrd-xen" % self.arch)]

    def _variantFromVersion(self):
        distro_version = self.version_from_content[1].strip()
        version = distro_version.split('.', 1)[0].strip()
        self.os_variant = self.urldistro
        if int(version) >= 10:
            if self.os_variant.startswith(("sles", "sled")):
                sp_version = None
                if len(distro_version.split('.', 1)) == 2:
                    sp_version = 'sp' + distro_version.split('.', 1)[1].strip()
                self.os_variant += version
                if sp_version:
                    self.os_variant += sp_version
            else:
                # Tumbleweed 8 digit date
                if len(version) == 8:
                    self.os_variant += "tumbleweed"
                else:
                    self.os_variant += distro_version
        else:
            self.os_variant += "9"

    def isValidStore(self):
        # self.version_from_content is the VERSION line from the contents file
        if (not self.version_from_content or
            self.version_from_content[1] is None):
            return False

        self._variantFromVersion()

        self.os_variant = self._detect_osdict_from_url()

        # Reset kernel name for sle11 source on s390x
        if self.arch == "s390x":
            if self.os_variant == "sles11" or self.os_variant == "sled11":
                self._hvm_kernel_paths = [("boot/%s/vmrdr.ikr" % self.arch,
                                           "boot/%s/initrd" % self.arch)]

        return True

    def _get_method_arg(self):
        return "install"

    ################################
    # osdict autodetection helpers #
    ################################

    def _detect_osdict_from_url(self):
        root = "opensuse"
        oses = [n for n in OSDB.list_os() if n.name.startswith(root)]

        for osobj in oses:
            codename = osobj.name[len(root):]
            if re.search("/%s/" % codename, self.uri):
                return osobj.name
        return self.os_variant


class SLESDistro(SuseDistro):
    urldistro = "sles"


class SLEDDistro(SuseDistro):
    urldistro = "sled"


# Suse  image store is harder - we fetch the kernel RPM and a helper
# RPM and then munge bits together to generate a initrd
class OpensuseDistro(SuseDistro):
    urldistro = "opensuse"


class DebianDistro(Distro):
    # ex. http://ftp.egr.msu.edu/debian/dists/sarge/main/installer-i386/
    # daily builds: http://d-i.debian.org/daily-images/amd64/
    name = "Debian"
    urldistro = "debian"

    def __init__(self, *args, **kwargs):
        Distro.__init__(self, *args, **kwargs)

        self._url_prefix = ""
        self._treeArch = self._find_treearch()
        self._installer_dirname = self.name.lower() + "-installer"

    def _find_treearch(self):
        for pattern in ["^.*/installer-(\w+)/?$",
                        "^.*/daily-images/(\w+)/?$"]:
            arch = re.findall(pattern, self.uri)
            if not arch:
                continue
            logging.debug("Found pattern=%s treearch=%s in uri",
                pattern, arch[0])
            return arch[0]

        # Check for standard 'i386' and 'amd64' which will be
        # in the URI name for --location $ISO mounts
        for arch in ["i386", "amd64", "x86_64", "arm64"]:
            if arch in self.uri:
                logging.debug("Found treearch=%s in uri", arch)
                if arch == "x86_64":
                    arch = "amd64"
                return arch

        # Otherwise default to i386
        arch = "i386"
        logging.debug("No treearch found in uri, defaulting to arch=%s", arch)
        return arch

    def _set_media_paths(self):
        self._boot_iso_paths   = ["%s/netboot/mini.iso" % self._url_prefix]

        hvmroot = "%s/netboot/%s/%s/" % (self._url_prefix,
                                         self._installer_dirname,
                                         self._treeArch)
        initrd_basename = "initrd.gz"
        kernel_basename = "linux"
        if self._treeArch in ["ppc64el"]:
            kernel_basename = "vmlinux"

        if self._treeArch == "s390x":
            hvmroot = "%s/generic/" % self._url_prefix
            kernel_basename = "kernel.%s" % self.name.lower()
            initrd_basename = "initrd.%s" % self.name.lower()

        self._hvm_kernel_paths = [
            (hvmroot + kernel_basename, hvmroot + initrd_basename)]

        xenroot = "%s/netboot/xen/" % self._url_prefix
        self._xen_kernel_paths = [(xenroot + "vmlinuz", xenroot + "initrd.gz")]

    def _check_manifest(self, filename):
        if not self.fetcher.hasFile(filename):
            return False

        if self.arch == "s390x":
            regex = ".*generic/kernel\.%s.*" % self.name.lower()
        else:
            regex = ".*%s.*" % self._installer_dirname

        if not self._fetchAndMatchRegex(filename, regex):
            logging.debug("Regex didn't match, not a %s distro", self.name)
            return False

        return True

    def _check_info(self, filename):
        if not self.fetcher.hasFile(filename):
            return False

        regex = "%s.*" % self.name

        if not self._fetchAndMatchRegex(filename, regex):
            logging.debug("Regex didn't match, not a %s distro", self.name)
            return False

        return True

    def _is_regular_tree(self):
        # For regular trees
        if not self._check_manifest("current/images/MANIFEST"):
            return False

        self._url_prefix = "current/images"
        self._set_media_paths()
        self.os_variant = self._detect_debian_osdict_from_url()

        return True

    def _is_daily_tree(self):
        # For daily trees
        if not self._check_manifest("daily/MANIFEST"):
            return False

        self._url_prefix = "daily"
        self._set_media_paths()
        self.os_variant = self._detect_debian_osdict_from_url()

        return True

    def _is_install_cd(self):
        # For install CDs
        if not self._check_info(".disk/info"):
            return False

        if self.arch == "x86_64":
            kernel_initrd_pair = ("install.amd/vmlinuz",
                                  "install.amd/initrd.gz")
        elif self.arch == "i686":
            kernel_initrd_pair = ("install.386/vmlinuz",
                                  "install.386/initrd.gz")
        elif self.arch == "aarch64":
            kernel_initrd_pair = ("install.a64/vmlinuz",
                                  "install.a64/initrd.gz")
        elif self.arch == "ppc64le":
            kernel_initrd_pair = ("install/vmlinux",
                                  "install/initrd.gz")
        elif self.arch == "s390x":
            kernel_initrd_pair = ("boot/linux_vm", "boot/root.bin")
        else:
            kernel_initrd_pair = ("install/vmlinuz", "install/initrd.gz")
        self._hvm_kernel_paths += [kernel_initrd_pair]
        self._xen_kernel_paths += [kernel_initrd_pair]

        return True

    def isValidStore(self):
        return any(check() for check in [
            self._is_regular_tree,
            self._is_daily_tree,
            self._is_install_cd,
            ])


    ################################
    # osdict autodetection helpers #
    ################################

    def _detect_debian_osdict_from_url(self):
        root = self.name.lower()
        oses = [n for n in OSDB.list_os() if n.name.startswith(root)]

        if self._url_prefix == "daily":
            logging.debug("Appears to be debian 'daily' URL, using latest "
                "debian OS")
            return oses[0].name

        for osobj in oses:
            if osobj.codename:
                # Ubuntu codenames look like 'Warty Warthog'
                codename = osobj.codename.split()[0].lower()
            else:
                if " " not in osobj.label:
                    continue
                # Debian labels look like 'Debian Sarge'
                codename = osobj.label.split()[1].lower()

            if ("/%s/" % codename) in self.uri:
                logging.debug("Found codename=%s in the URL string", codename)
                return osobj.name

        logging.debug("Didn't find any known codename in the URL string")
        return self.os_variant


class UbuntuDistro(DebianDistro):
    # http://archive.ubuntu.com/ubuntu/dists/natty/main/installer-amd64/
    name = "Ubuntu"
    urldistro = "ubuntu"

    def _is_tree_iso(self):
        # For trees based on ISO's
        if not self._check_info("install/netboot/version.info"):
            return False

        self._url_prefix = "install"
        self._set_media_paths()
        self.os_variant = self._detect_debian_osdict_from_url()

        return True

    def _is_install_cd(self):
        # For install CDs
        if not self._check_info(".disk/info"):
            return False

        if not self.arch == "s390x":
            kernel_initrd_pair = ("install/vmlinuz", "install/initrd.gz")
        else:
            kernel_initrd_pair = ("boot/kernel.ubuntu", "boot/initrd.ubuntu")

        self._hvm_kernel_paths += [kernel_initrd_pair]
        self._xen_kernel_paths += [kernel_initrd_pair]

        return True



class MandrivaDistro(Distro):
    # ftp://ftp.uwsg.indiana.edu/linux/mandrake/official/2007.1/x86_64/
    name = "Mandriva/Mageia"
    urldistro = "mandriva"

    _boot_iso_paths = ["install/images/boot.iso"]
    _xen_kernel_paths = []

    def __init__(self, *args, **kwargs):
        Distro.__init__(self, *args, **kwargs)
        self._hvm_kernel_paths = []

        # At least Mageia 5 uses arch in the names
        self._hvm_kernel_paths += [
            ("isolinux/%s/vmlinuz" % self.arch,
             "isolinux/%s/all.rdz" % self.arch)]

        # Kernels for HVM: valid for releases 2007.1, 2008.*, 2009.0
        self._hvm_kernel_paths += [
            ("isolinux/alt0/vmlinuz", "isolinux/alt0/all.rdz")]


    def isValidStore(self):
        # Don't support any paravirt installs
        if self.type is not None and self.type != "hvm":
            return False

        # Mandriva websites / media appear to have a VERSION
        # file in top level which we can use as our 'magic'
        # check for validity
        if not self.fetcher.hasFile("VERSION"):
            return False

        for name in ["Mandriva", "Mageia"]:
            if self._fetchAndMatchRegex("VERSION", ".*%s.*" % name):
                return True

        logging.debug("Regex didn't match, not a %s distro", self.name)
        return False


class ALTLinuxDistro(Distro):
    # altlinux doesn't have installable URLs, so this is just for a
    # mounted ISO
    name = "ALT Linux"
    urldistro = "altlinux"

    _boot_iso_paths = [("altinst", "live")]
    _hvm_kernel_paths = [("syslinux/alt0/vmlinuz", "syslinux/alt0/full.cz")]
    _xen_kernel_paths = []

    def isValidStore(self):
        # Don't support any paravirt installs
        if self.type is not None and self.type != "hvm":
            return False

        if not self.fetcher.hasFile(".disk/info"):
            return False

        if self._fetchAndMatchRegex(".disk/info", ".*ALT .*"):
            return True

        logging.debug("Regex didn't match, not a %s distro", self.name)
        return False


# Build list of all *Distro classes
def _build_distro_list():
    allstores = []
    for obj in list(globals().values()):
        if isinstance(obj, type) and issubclass(obj, Distro) and obj.name:
            allstores.append(obj)

    seen_urldistro = []
    for obj in allstores:
        if obj.urldistro and obj.urldistro in seen_urldistro:
            raise RuntimeError("programming error: duplicate urldistro=%s" %
                               obj.urldistro)
        seen_urldistro.append(obj.urldistro)

    # Always stick GenericDistro at the end, since it's a catchall
    allstores.remove(GenericDistro)
    allstores.append(GenericDistro)

    return allstores

_allstores = _build_distro_list()