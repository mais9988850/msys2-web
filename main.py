#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright 2016 Christoph Reiter
#
# Permission is hereby granted, free of charge, to any person obtaining
# a copy of this software and associated documentation files (the
# "Software"), to deal in the Software without restriction, including
# without limitation the rights to use, copy, modify, merge, publish,
# distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so, subject to
# the following conditions:
#
# The above copyright notice and this permission notice shall be included
# in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
# IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY
# CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT,
# TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
# SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

import argparse
import traceback
from urllib.parse import quote
import contextlib
import datetime
import io
import re
import os
import sys
import tarfile
import threading
import time
import json
import subprocess
from itertools import zip_longest
from functools import cmp_to_key
from urllib.parse import quote_plus

import requests
from flask import Flask, render_template, request, url_for, redirect


CONFIG = [
    ("http://repo.msys2.org/mingw/i686/mingw32.files", "mingw32", ""),
    ("http://repo.msys2.org/mingw/x86_64/mingw64.files", "mingw64", ""),
    ("http://repo.msys2.org/msys/i686/msys.files", "msys", "i686"),
    ("http://repo.msys2.org/msys/x86_64/msys.files", "msys", "x86_64"),
]

VERSION_CONFIG = []
for repo in ["core", "extra", "community", "testing", "community-testing",
             "multilib"]:
    VERSION_CONFIG.append(
        ("http://ftp.halifax.rwth-aachen.de/archlinux/"
         "{0}/os/x86_64/{0}.db".format(repo), repo, ""))

SRCINFO_CONFIG = [
    ("https://github.com/lazka/msys2-web/releases/download/cache/srcinfo.json",
     "", "")
]

UPDATE_INTERVAL = 60 * 5
REQUEST_TIMEOUT = 60

sources = []
sourceinfos = {}
versions = {}
last_update = 0

app = Flask(__name__)

app.config["CACHE_LOCAL"] = False


def parse_desc(t):
    d = {}
    cat = None
    values = []
    for l in t.splitlines():
        l = l.strip()
        if not l:
            d[cat] = values
            cat = None
            values = []
        elif cat is None:
            cat = l
        else:
            values.append(l)
    if cat is not None:
        d[cat] = values
    return d


def cleanup_files(files):
    """Remove redundant directory paths and root them"""

    last = None
    result = []
    for path in sorted(files, reverse=True):
        if last is not None:
            if path.endswith("/") and last.startswith(path):
                continue
        result.append("/" + path)
        last = path
    return result[::-1]


class Package:

    def __init__(self, builddate, csize, depends, filename, files, isize,
                 makedepends, md5sum, name, pgpsig, sha256sum, arch,
                 base_url, repo, repo_variant, provides, conflicts, replaces,
                 version, base, desc, groups, licenses, optdepends,
                 checkdepends):
        self.builddate = int(builddate)
        self.csize = csize

        def split_depends(deps):
            r = []
            for d in deps:
                parts = re.split("([<>=]+)", d, 1)
                first = parts[0].strip()
                second = "".join(parts[1:]).strip()
                r.append([first, second])
            return r

        self.depends = split_depends(depends)
        self.checkdepends = split_depends(checkdepends)
        self.filename = filename
        self.files = cleanup_files(files)
        self.isize = isize
        self.makedepends = split_depends(makedepends)
        self.md5sum = md5sum
        self.name = name
        self.pgpsig = pgpsig
        self.sha256sum = sha256sum
        self.arch = arch
        self.fileurl = base_url + "/" + quote(self.filename)
        self.repo = repo
        self.repo_variant = repo_variant
        self.provides = dict(split_depends(provides))
        self.conflicts = conflicts
        self.replaces = replaces
        self.version = version
        self.base = base
        self.desc = desc
        self.groups = groups
        self.licenses = licenses
        self.rdepends = []

        def split_opt(deps):
            r = []
            for d in deps:
                if ":" in d:
                    r.append([p.strip() for p in d.split(":", 1)])
                else:
                    r.append([d.strip(), ""])
            return r

        self.optdepends = split_opt(optdepends)

    def __repr__(self):
        return "Package(%s)" % self.fileurl

    @property
    def realprovides(self):
        prov = {}
        for key, info in self.provides.items():
            if key.startswith("mingw"):
                key = key.split("-", 3)[-1]
            prov[key] = info
        return prov

    @property
    def realname(self):
        if self.repo.startswith("mingw"):
            return self.name.split("-", 3)[-1]
        return self.name

    @property
    def git_version(self):
        if self.name in sourceinfos:
            return sourceinfos[self.name].build_version
        return ""

    @property
    def key(self):
        return (self.repo, self.repo_variant,
                self.name, self.arch, self.fileurl)

    @classmethod
    def from_desc(cls, d, base, base_url, repo, repo_variant):
        return cls(d["%BUILDDATE%"][0], d["%CSIZE%"][0],
                   d.get("%DEPENDS%", []), d["%FILENAME%"][0],
                   d.get("%FILES%", []), d["%ISIZE%"][0],
                   d.get("%MAKEDEPENDS%", []),
                   d["%MD5SUM%"][0], d["%NAME%"][0],
                   d.get("%PGPSIG%", [""])[0], d["%SHA256SUM%"][0],
                   d["%ARCH%"][0], base_url, repo, repo_variant,
                   d.get("%PROVIDES%", []), d.get("%CONFLICTS%", []),
                   d.get("%REPLACES%", []), d["%VERSION%"][0], base,
                   d.get("%DESC%", [""])[0], d.get("%GROUPS%", []),
                   d.get("%LICENSE%", []), d.get("%OPTDEPENDS%", []),
                   d.get("%CHECKDEPENDS%", []))


class Source:

    def __init__(self, name, desc, url, packager, repo,
                 repo_variant):
        self.name = name
        self.desc = desc
        self.url = url
        self.packager = packager
        self._repo = repo
        self._repo_variant = repo_variant

        self.packages = {}

    @property
    def repos(self):
        return sorted(set([p.repo for p in self.packages.values()]))

    @property
    def arches(self):
        return sorted(set([p.arch for p in self.packages.values()]))

    @property
    def groups(self):
        groups = set()
        for p in self.packages.values():
            groups.update(p.groups)
        return sorted(groups)

    @property
    def version(self):
        # get the newest version
        versions = set([p.version for p in self.packages.values()])
        versions = sorted(versions, key=cmp_to_key(vercmp), reverse=True)
        return versions[0]

    @property
    def git_version(self):
        # get the newest version
        versions = set([p.git_version for p in self.packages.values()])
        versions = sorted(versions, key=cmp_to_key(vercmp), reverse=True)
        return versions[0]

    @property
    def licenses(self):
        licenses = set()
        for p in self.packages.values():
            licenses.update(p.licenses)
        return sorted(licenses)

    @property
    def arch_url(self):
        arch_info = get_arch_info_for_base(self)
        if arch_info is not None:
            return arch_info[1]
        return ""

    @property
    def upstream_version(self):
        arch_info = get_arch_info_for_base(self)
        if arch_info is not None:
            return extract_upstream_version(arch_info[0])
        return ""

    @property
    def is_outdated(self):
        arch_version = self.upstream_version
        if not arch_version:
            return False

        msys_version = extract_upstream_version(self.version)

        return version_is_newer_than(arch_version, msys_version)

    @property
    def realname(self):
        if self._repo.startswith("mingw"):
            return self.name.split("-", 2)[-1]
        return self.name

    @property
    def date(self):
        """The build date of the newest package"""

        return sorted([p.builddate for p in self.packages.values()])[-1]

    @property
    def repo_url(self):
        if self._repo.startswith("mingw"):
            return "https://github.com/Alexpux/MINGW-packages"
        else:
            return "https://github.com/Alexpux/MSYS2-packages"

    @property
    def source_url(self):
        return self.repo_url + ("/tree/master/" + quote_plus(self.name))

    @property
    def history_url(self):
        return self.repo_url + ("/commits/master/" + quote_plus(self.name))

    @property
    def filebug_url(self):
        name = self.name
        if name.startswith("mingw-w64-"):
            name = name.split("-", 2)[-1]

        return self.repo_url + (
            "/issues/new?title=" + quote_plus("[%s]" % name))

    @property
    def searchbug_url(self):
        name = self.name
        if name.startswith("mingw-w64-"):
            name = name.split("-", 2)[-1]

        return self.repo_url + (
            "/issues?q=" + quote_plus("is:issue is:open %s" % name))

    @classmethod
    def from_desc(cls, d, repo, repo_variant):

        name = d["%NAME%"][0]
        if "%BASE%" not in d:
            if repo.startswith("mingw"):
                base = "mingw-w64-" + name.split("-", 3)[-1]
            else:
                base = name
        else:
            base = d["%BASE%"][0]

        return cls(base, d.get("%DESC%", [""])[0], d.get("%URL%", [""])[0],
                   d["%PACKAGER%"][0], repo, repo_variant)

    def add_desc(self, d, base_url):
        p = Package.from_desc(
            d, self.name, base_url, self._repo, self._repo_variant)
        assert p.key not in self.packages
        self.packages[p.key] = p


def parse_repo(repo, repo_variant, url):
    base_url = url.rsplit("/", 1)[0]
    sources = {}
    print("Loading %r" % url)

    def add_desc(d, base_url):
        source = Source.from_desc(d, repo, repo_variant)
        if source.name not in sources:
            sources[source.name] = source
        else:
            source = sources[source.name]

        source.add_desc(d, base_url)

    if app.config["CACHE_LOCAL"]:
        fn = url.replace("/", "_").replace(":", "_")
        if not os.path.exists(fn):
            r = requests.get(url, timeout=REQUEST_TIMEOUT)
            with open(fn, "wb") as h:
                h.write(r.content)
        with open(fn, "rb") as h:
            data = h.read()
    else:
        r = requests.get(url, timeout=REQUEST_TIMEOUT)
        data = r.content

    with io.BytesIO(data) as f:
        with tarfile.open(fileobj=f, mode="r:gz") as tar:
            packages = {}
            for info in tar.getmembers():
                package_name = info.name.split("/", 1)[0]
                infofile = tar.extractfile(info)
                if infofile is None:
                    continue
                with infofile:
                    packages.setdefault(package_name, []).append(
                        (info.name, infofile.read()))

    for package_name, infos in sorted(packages.items()):
        t = ""
        for name, data in sorted(infos):
            if name.endswith("/desc"):
                t += data.decode("utf-8")
            if name.endswith("/files"):
                t += data.decode("utf-8")
        desc = parse_desc(t)
        add_desc(desc, base_url)

    return sources


@app.template_filter('timestamp')
def _jinja2_filter_timestamp(d):
    try:
        return datetime.datetime.fromtimestamp(
            int(d)).strftime('%Y-%m-%d %H:%M:%S')
    except OSError:
        return "-"


@app.template_filter('filesize')
def _jinja2_filter_filesize(d):
    return "%.2f MB" % (int(d) / (1024.0 ** 2))


@app.context_processor
def funcs():

    def package_url(package, name=None):
        if name is None:
            res = url_for("package", name=name or package.name)
            res += "?repo=" + package.repo
            if package.repo_variant:
                res += "&variant=" + package.repo_variant
        else:
            res = url_for("package", name=re.split("[<>=]+", name)[0])
            if package.repo_variant:
                res += "?repo=" + package.repo
                res += "&variant=" + package.repo_variant
        return res

    def package_name(package, name=None):
        name = name or package.name
        name = re.split("[<>=]+", name, 1)[0]
        return (name or package.name) + (
            "/" + package.repo_variant if package.repo_variant else "")

    def package_restriction(package, name=None):
        name = name or package.name
        return name[len(re.split("[<>=]+", name)[0]):].strip()

    def update_timestamp():
        return last_update

    return dict(package_url=package_url, package_name=package_name,
                package_restriction=package_restriction,
                update_timestamp=update_timestamp)


@app.route('/repos')
def repos():
    return render_template('packages/repos.html')


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/base')
@app.route('/base/<name>')
def base(name=None):
    global sources

    if name is not None:
        res = [s for s in sources if s.name == name]
        return render_template('packages/base.html', sources=res)
    else:
        return render_template('packages/baseindex.html', sources=sources)


@app.route('/group/')
@app.route('/group/<name>')
def group(name=None):
    global sources

    if name is not None:
        res = []
        for s in sources:
            for k, p in sorted(s.packages.items()):
                if name in p.groups:
                    res.append(p)

        return render_template('packages/group.html', name=name, packages=res)
    else:
        groups = {}
        for s in sources:
            for k, p in sorted(s.packages.items()):
                for name in p.groups:
                    groups[name] = groups.get(name, 0) + 1
        return render_template('packages/groups.html', groups=groups)


@app.route('/package/<name>')
def package(name):
    global sources

    repo = request.args.get('repo')
    variant = request.args.get('variant')

    packages = []
    for s in sources:
        for k, p in sorted(s.packages.items()):
            if p.name == name or name in p.provides:
                if not repo or p.repo == repo:
                    if not variant or p.repo_variant == variant:
                        packages.append(p)
    return render_template('packages/package.html', packages=packages)


@app.route('/updates')
def updates():
    global sources

    packages = []
    for s in sources:
        packages.extend(s.packages.values())
    packages.sort(key=lambda p: p.builddate, reverse=True)
    return render_template('packages/updates.html', packages=packages[:150])


def package_name_is_vcs(package_name):
    return package_name.endswith(
        ("-cvs", "-svn", "-hg", "-darcs", "-bzr", "-git"))


def get_arch_name(name):
    mapping = {
        "freetype": "freetype2",
        "lzo2": "lzo",
        "liblzo2": "lzo",
        "python-bsddb3": "python-bsddb",
        "graphite2": "graphite",
        "mpc": "libmpc",
        "eigen3": "eigen",
        "python-icu": "python-pyicu",
        "python-bsddb3": "python-bsddb",
        "python3": "python",
        "sqlite3": "sqlite",
        "gexiv2": "libgexiv2",
        "webkitgtk3": "webkitgtk",
        "python2-nuitka": "nuitka",
        "python2-ipython": "ipython",
        "openssl": "openssl-1.0",
        "gtksourceviewmm3": "gtksourceviewmm",
        "librest": "rest",
        "gcc-libgfortran": "gcc-fortran",
        "meld3": "meld",
        "antlr3": "libantlr3c",
        "geoclue": "geoclue2",
        "python-zope.event": "python-zope-event",
        "python-zope.interface": "python-zope-interface",
        "tesseract-ocr": "tesseract",
        "cmake-doc-qt": "cmake",
        "totem-pl-parser": "totem-plparser",
        "vulkan-docs": "vulkan-headers",
        "vulkan": "vulkan-headers",
        "qt-creator": "qtcreator",
        "qt5": "qt5-base",
        "qt5-static": "qt5-base",
        "quassel": "quassel-client",
        "spice-gtk": "spice-gtk3",
        "libbotan": "botan",
        "shiboken-qt4": "shiboken",
        "python-ipython": "ipython",
        "glob": "google-glog",
        "lsqlite3": "lua-sql-sqlite",
        "fdk-aac": "libfdk-aac",
        "python-jupyter_console": "jupyter_console",
        "qscintilla": "qscintilla-qt5",
        "attica-qt5": "attica",
        "glade3": "glade-gtk2",
        "ladspa-sdk": "ladspa",
        "libart_lgpl": "libart-lgpl",
        "ocaml-camlp4": "camlp4",
        "wxwidgets": "wxgtk3",
        "transmission": "transmission-gtk",
        "perl-ack": "ack",
        "glfw": "glfw-x11",
        "util-macros": "xorg-util-macros",
        "tzcode": "tzdata",
        "glog": "google-glog",
        "git-flow": "gitflow-avh",
        "rabbitmq-c": "librabbitmq-c",
        "usrsctp": "libusrsctp",
        "matio": "libmatio",
        "libgd": "gd",
        "python-nbformat": "jupyter-nbformat",
    }

    name = name.lower()

    if name.startswith("python3-"):
        name = name.replace("python3-", "python-")

    if name.startswith("mingw-w64-cross-"):
        name = name.split("-", 3)[-1]

    if name.endswith("-qt5") or name.endswith("-qt4"):
        name = name.rsplit("-", 1)[0]

    if name in mapping:
        return mapping[name]

    return name


def is_win_only(name):
    win_only = set([
        "winpty",
        "windows-default-manifest",
        "mingw-w64-cross-windows-default-manifest",
        "mingw-w64-MinHook",
        "msys2-w32api-headers",
        "mintty",
        "mingw-w64-python-win_unicode_console",
        "msys2-keyring",
        "cygrunsrv",
        "mingw-w64-cccl",
        "mingw-w64-dlfcn",
        "mingw-w64-drmingw",
        "mingw-w64-edd-dbg",
        "mingw-w64-editrights",
        "mingw-w64-flexdll",
        "winln",
        "rebase",
        "msys2-w32api-runtime",
        "msys2-runtime",
        "mingw-w64-win7appid",
        "mingw-w64-windows-default-manifest",
        "mingw-w64-wineditline",
        "mingw-w64-winico",
        "mingw-w64-winsparkle",
        "crypt",
        "pacman-mirrors",
    ])

    return name in win_only


def vercmp(v1, v2):

    def cmp(a, b):
        return (a > b) - (a < b)

    def split(v):
        e, v = v.split("~", 1) if "~" in v else ("0", v)
        v, r = v.rsplit("-", 1) if "-" in v else (v, None)
        return (e, v, r)

    digit, alpha, other = range(3)

    def get_type(c):
        assert c
        if c.isdigit():
            return digit
        elif c.isalpha():
            return alpha
        else:
            return other

    def parse(v):
        parts = []
        seps = 0
        current = ""
        for c in v:
            if get_type(c) == other:
                if current:
                    parts.append((seps, current))
                    current = ""
                seps += 1
            else:
                if not current:
                    current += c
                else:
                    if get_type(c) == get_type(current):
                        current += c
                    else:
                        parts.append((seps, current))
                        current = c

        parts.append((seps, current or None))

        return parts

    def rpmvercmp(v1, v2):
        for (s1, p1), (s2, p2) in zip_longest(parse(v1), parse(v2),
                                              fillvalue=(None, None)):

            if s1 is not None and s2 is not None:
                ret = cmp(s1, s2)
                if ret != 0:
                    return ret

            if p1 is None and p2 is None:
                return 0

            if p1 is None:
                if get_type(p2) == alpha:
                    return 1
                return -1
            elif p2 is None:
                if get_type(p1) == alpha:
                    return -1
                return 1

            t1 = get_type(p1)
            t2 = get_type(p2)
            if t1 != t2:
                if t1 == digit:
                    return 1
                elif t2 == digit:
                    return -1
            elif t1 == digit:
                ret = cmp(int(p1), int(p2))
                if ret != 0:
                    return ret
            elif t1 == alpha:
                ret = cmp(p1, p2)
                if ret != 0:
                    return ret

        return 0

    e1, v1, r1 = split(v1)
    e2, v2, r2 = split(v2)

    ret = rpmvercmp(e1, e2)
    if ret == 0:
        ret = rpmvercmp(v1, v2)
        if ret == 0 and r1 is not None and r2 is not None:
            ret = rpmvercmp(r1, r2)

    return ret


def arch_version_to_msys(v):
    return v.replace(":", "~")


def version_is_newer_than(v1, v2):
    return vercmp(v1, v2) == 1


def update_versions():
    global VERSION_CONFIG, versions, sources

    print("update versions")
    arch_versions = {}
    for (url, repo, variant) in VERSION_CONFIG:
        for source in parse_repo(repo, variant, url).values():
            msys_ver = arch_version_to_msys(source.version)
            for p in source.packages.values():
                url = "https://www.archlinux.org/packages/%s/%s/%s/" % (
                    p.repo, p.arch, p.name)

                if p.name in arch_versions:
                    old_ver = arch_versions[p.name][0]
                    if version_is_newer_than(msys_ver, old_ver):
                        arch_versions[p.name] = (msys_ver, url, p.builddate)
                else:
                    arch_versions[p.name] = (msys_ver, url, p.builddate)

            url = "https://www.archlinux.org/packages/%s/%s/%s/" % (
                    source.repos[0], source.arches[0], source.name)
            if source.name in arch_versions:
                old_ver = arch_versions[source.name][0]
                if version_is_newer_than(msys_ver, old_ver):
                    arch_versions[source.name] = (msys_ver, url, source.date)
            else:
                arch_versions[source.name] = (msys_ver, url, source.date)

    print("done")

    print("update versions from AUR")
    # a bit hacky, try to get the remaining versions from AUR
    possible_names = set()
    for s in sources:
        if package_name_is_vcs(s.name):
            continue
        for p in s.packages.values():
            possible_names.add(get_arch_name(p.realname))
        possible_names.add(get_arch_name(s.realname))

    r = requests.get("https://aur.archlinux.org/packages.gz",
                     timeout=REQUEST_TIMEOUT)
    aur_packages = set()
    for name in r.text.splitlines():
        if name.startswith("#"):
            continue
        if name in arch_versions:
            continue
        if name not in possible_names:
            continue
        aur_packages.add(name)

    aur_url = (
        "https://aur.archlinux.org/rpc/?v=5&type=info&" +
        "&".join(["arg[]=%s" % n for n in aur_packages]))
    r = requests.get(aur_url, timeout=REQUEST_TIMEOUT)
    for result in r.json()["results"]:
        name = result["Name"]
        if name not in aur_packages or name in arch_versions:
            continue
        last_modified = result["LastModified"]
        url = "https://aur.archlinux.org/packages/%s" % name
        arch_versions[name] = (result["Version"], url, last_modified)
    print("done")

    versions = arch_versions


def extract_upstream_version(version):
    return version.rsplit(
        "-")[0].split("+", 1)[0].split("~", 1)[-1].split(":", 1)[-1]


def get_arch_info_for_base(s):
    """tuple or None"""

    global versions

    variants = sorted([s.realname] + [p.realname for p in s.packages.values()])

    # fallback to the provide names
    provides_variants = []
    for p in s.packages.values():
        provides_variants.extend(p.realprovides.keys())
    variants += provides_variants

    for realname in variants:
        arch_name = get_arch_name(realname)
        if arch_name in versions:
            return tuple(versions[arch_name])


@app.route('/outofdate')
def outofdate():
    global sources, versions

    missing = []
    win_only = []
    to_update = []
    all_sources = []
    for s in sources:
        if package_name_is_vcs(s.name):
            continue

        all_sources.append(s)

        arch_info = get_arch_info_for_base(s)
        if arch_info is None:
            if is_win_only(s.name):
                win_only.append(s)
            else:
                missing.append((s, get_arch_name(s.realname)))
            continue

        arch_version, url, date = arch_info
        arch_version = extract_upstream_version(arch_version)
        msys_version = extract_upstream_version(s.version)

        if version_is_newer_than(arch_version, msys_version):
            to_update.append((s, msys_version, arch_version, url, date))

    # show packages which have recently been build first.
    # assumes high frequency update packages are more important
    to_update.sort(key=lambda i: (i[-1], i[0].name), reverse=True)

    missing.sort(key=lambda i: i[0].name)
    win_only.sort(key=lambda i: i.name)

    return render_template(
        'packages/outofdate.html',
        all_sources=all_sources, to_update=to_update, missing=missing,
        win_only=win_only)


@app.route('/queue')
def queue():
    global sources, sourceinfos

    # get all packages in the pacman repo which are no in GIT
    missing = []
    for s in sources:
        for k, p in s.packages.items():
            if p.name not in sourceinfos:
                missing.append((s, p))
    missing.sort(key=lambda i: (i[1].builddate, i[1].name), reverse=True)

    # Create dummy entries for all GIT only packages
    available = {}
    for srcinfo in sourceinfos.values():
        if package_name_is_vcs(srcinfo.pkgbase):
            continue
        available[srcinfo.pkgbase] = (srcinfo, None, None)
    for s in sources:
        available.pop(s.name, None)
    outofdate = list(available.values())

    # Create entries for all packages where the version doesn't match
    for s in sources:
        for k, p in sorted(s.packages.items()):
            if p.name in sourceinfos:
                srcinfo = sourceinfos[p.name]
                if package_name_is_vcs(s.name):
                    continue
                if p.version != srcinfo.build_version:
                    outofdate.append((srcinfo, s, p))
                    break

    outofdate.sort(
        key=lambda i: (i[0].date, i[0].pkgbase, i[0].pkgname),
        reverse=True)

    return render_template(
        'packages/queue.html', outofdate=outofdate, new=[], missing=missing)


@app.route('/search')
def search():
    global sources

    query = request.args.get('q', '')
    qtype = request.args.get('t', '')

    if qtype not in ["pkg"]:
        qtype = "pkg"

    parts = query.split()
    res_pkg = []

    if not query:
        pass
    elif qtype == "pkg":
        for s in sources:
            if [p for p in parts if p.lower() in s.name.lower()] == parts:
                res_pkg.append(s)

    res_pkg.sort(key=lambda s: s.name)

    return render_template(
        'packages/search.html', sources=res_pkg, query=query, qtype=qtype)


@contextlib.contextmanager
def check_needs_update(_last_time=[""]):
    """Raises RequestException"""

    if app.config["CACHE_LOCAL"]:
        yield True
        return

    t = ""
    for config in sorted(CONFIG + VERSION_CONFIG + SRCINFO_CONFIG):
        url = config[0]
        r = requests.get(url, stream=True, timeout=REQUEST_TIMEOUT)
        r.close()
        t += r.headers["last-modified"]

    if t != _last_time[0]:
        yield True
        _last_time[0] = t
    else:
        yield False


def update_source():
    """Raises RequestException"""

    global sources, CONFIG

    print("update source")

    final = {}
    for (url, repo, variant) in CONFIG:
        for name, source in parse_repo(repo, variant, url).items():
            if name in final:
                final[name].packages.update(source.packages)
            else:
                final[name] = source

    sources = [x[1] for x in sorted(final.items())]
    fill_rdepends(sources)


def update_sourceinfos():
    global sourceinfos, SRCINFO_CONFIG

    print("update sourceinfos")

    url = SRCINFO_CONFIG[0][0]
    print("Loading %r" % url)

    if app.config["CACHE_LOCAL"]:
        fn = url.replace("/", "_").replace(":", "_")
        if not os.path.exists(fn):
            r = requests.get(url, timeout=REQUEST_TIMEOUT)
            with open(fn, "wb") as h:
                h.write(r.content)
        with open(fn, "rb") as h:
            data = h.read()
    else:
        r = requests.get(url, timeout=REQUEST_TIMEOUT)
        data = r.content

    json_obj = json.loads(data.decode("utf-8"))
    result = {}
    items = sorted(json_obj.items(), key=lambda i: i[1])
    for hash_, (srcinfo, repo, date) in items:
        for pkg in SrcInfoPackage.for_srcinfo(srcinfo, repo, date):
            result[pkg.pkgname] = pkg

    sourceinfos = result


def fill_rdepends(sources):
    deps = {}
    for s in sources:
        for p in s.packages.values():
            for n, r in p.depends:
                deps.setdefault(n, set()).add((p, ""))
            for n, r in p.makedepends:
                deps.setdefault(n, set()).add((p, "make"))
            for n, r in p.optdepends:
                deps.setdefault(n, set()).add((p, "optional"))
            for n, r in p.checkdepends:
                deps.setdefault(n, set()).add((p, "check"))

    for s in sources:
        for p in s.packages.values():
            rdepends = list(deps.get(p.name, []))
            for prov in p.provides:
                rdepends += list(deps.get(prov, []))
            p.rdepends = sorted(rdepends, key=lambda e: (e[0].key, e[1]))

            # filter out other arches for msys packages
            if p.repo_variant:
                p.rdepends = [
                    (op, t) for (op, t) in p.rdepends if
                    op.repo_variant in (p.repo_variant, "")]


def update_thread():
    global sources, UPDATE_INTERVAL, last_update

    while True:
        try:
            print("check for update")
            with check_needs_update() as needs:
                if needs:
                    update_source()
                    update_sourceinfos()
                    update_versions()
                else:
                    print("not update needed")
        except Exception:
            traceback.print_exc()
        else:
            last_update = time.time()
        print("Sleeping for %d" % UPDATE_INTERVAL)
        time.sleep(UPDATE_INTERVAL)


thread = threading.Thread(target=update_thread)
thread.daemon = True
thread.start()


class SrcInfoPackage(object):

    def __init__(self, pkgbase, pkgname, pkgver, pkgrel, repo, date):
        self.pkgbase = pkgbase
        self.pkgname = pkgname
        self.pkgver = pkgver
        self.pkgrel = pkgrel
        self.repo_url = repo
        self.date = date
        self.epoch = None
        self.depends = []
        self.makedepends = []
        self.sources = []

    @property
    def history_url(self):
        return self.repo_url + ("/commits/master/" + quote_plus(self.pkgbase))

    @property
    def source_url(self):
        return self.repo_url + ("/tree/master/" + quote_plus(self.pkgbase))

    @property
    def build_version(self):
        version = "%s-%s" % (self.pkgver, self.pkgrel)
        if self.epoch:
            version = "%s~%s" % (self.epoch, version)
        return version

    def __repr__(self):
        return "<%s %s %s>" % (
            type(self).__name__, self.pkgname, self.build_version)

    @classmethod
    def for_srcinfo(cls, srcinfo, repo, date):
        packages = set()

        for line in srcinfo.splitlines():
            line = line.strip()
            if line.startswith("pkgbase = "):
                pkgver = pkgrel = epoch = None
                depends = []
                makedepends = []
                sources = []
                pkgbase = line.split(" = ", 1)[-1]
            elif line.startswith("depends = "):
                depends.append(line.split(" = ", 1)[-1])
            elif line.startswith("makedepends = "):
                makedepends.append(line.split(" = ", 1)[-1])
            elif line.startswith("source = "):
                sources.append(line.split(" = ", 1)[-1])
            elif line.startswith("pkgver = "):
                pkgver = line.split(" = ", 1)[-1]
            elif line.startswith("pkgrel = "):
                pkgrel = line.split(" = ", 1)[-1]
            elif line.startswith("epoch = "):
                epoch = line.split(" = ", 1)[-1]
            elif line.startswith("pkgname = "):
                pkgname = line.split(" = ", 1)[-1]
                package = cls(pkgbase, pkgname, pkgver, pkgrel, repo, date)
                package.epoch = epoch
                package.depends = depends
                package.makedepends = makedepends
                package.sources = sources
                packages.add(package)

        return packages


def main(argv):
    from twisted.internet import reactor
    from twisted.web.server import Site
    from twisted.web.wsgi import WSGIResource
    from twisted.python import log

    parser = argparse.ArgumentParser()
    parser.add_argument("-c", "--cache", action="store_true",
                        help="use local repo cache")
    parser.add_argument("-p", "--port", type=int, default=8160,
                        help="port number")
    parser.add_argument("-d", "--debug", action="store_true")
    args = parser.parse_args()

    app.config["CACHE_LOCAL"] = args.cache
    print("http://localhost:%d" % args.port)

    if args.debug:
        app.debug=True
        log.startLogging(sys.stdout)

    wsgiResource = WSGIResource(reactor, reactor.getThreadPool(), app)
    site = Site(wsgiResource)
    reactor.listenTCP(args.port, site)
    reactor.run()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
