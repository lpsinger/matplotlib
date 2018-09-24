"""
A module for finding, managing, and using fonts across platforms.

This module provides a single :class:`FontManager` instance that can
be shared across backends and platforms.  The :func:`findfont`
function returns the best TrueType (TTF) font file in the local or
system font path that matches the specified :class:`FontProperties`
instance.  The :class:`FontManager` also handles Adobe Font Metrics
(AFM) font files for use by the PostScript backend.

The design is based on the `W3C Cascading Style Sheet, Level 1 (CSS1)
font specification <http://www.w3.org/TR/1998/REC-CSS2-19980512/>`_.
Future versions may implement the Level 2 or 2.1 specifications.

Experimental support is included for using `fontconfig` on Unix
variant platforms (Linux, OS X, Solaris).  To enable it, set the
constant ``USE_FONTCONFIG`` in this file to ``True``.  Fontconfig has
the advantage that it is the standard way to look up fonts on X11
platforms, so if a font is installed, it is much more likely to be
found.
"""

# KNOWN ISSUES
#
#   - documentation
#   - font variant is untested
#   - font stretch is incomplete
#   - font size is incomplete
#   - default font algorithm needs improvement and testing
#   - setWeights function needs improvement
#   - 'light' is an invalid weight value, remove it.
#   - update_fonts not implemented

from functools import lru_cache
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
try:
    from threading import Timer
except ImportError:
    from dummy_threading import Timer
import warnings

import matplotlib as mpl
from matplotlib import afm, cbook, ft2font, rcParams, get_cachedir
from matplotlib.fontconfig_pattern import (
    parse_fontconfig_pattern, generate_fontconfig_pattern)

_log = logging.getLogger(__name__)

USE_FONTCONFIG = False

font_scalings = {
    'xx-small' : 0.579,
    'x-small'  : 0.694,
    'small'    : 0.833,
    'medium'   : 1.0,
    'large'    : 1.200,
    'x-large'  : 1.440,
    'xx-large' : 1.728,
    'larger'   : 1.2,
    'smaller'  : 0.833,
    None       : 1.0}

stretch_dict = {
    'ultra-condensed' : 100,
    'extra-condensed' : 200,
    'condensed'       : 300,
    'semi-condensed'  : 400,
    'normal'          : 500,
    'semi-expanded'   : 600,
    'expanded'        : 700,
    'extra-expanded'  : 800,
    'ultra-expanded'  : 900}

weight_dict = {
    'ultralight' : 100,
    'light'      : 200,
    'normal'     : 400,
    'regular'    : 400,
    'book'       : 400,
    'medium'     : 500,
    'roman'      : 500,
    'semibold'   : 600,
    'demibold'   : 600,
    'demi'       : 600,
    'bold'       : 700,
    'heavy'      : 800,
    'extra bold' : 800,
    'black'      : 900}

font_family_aliases = {
    'serif',
    'sans-serif',
    'sans serif',
    'cursive',
    'fantasy',
    'monospace',
    'sans'}

#  OS Font paths
MSFolders = \
    r'Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders'


MSFontDirectories = [
    r'SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts',
    r'SOFTWARE\Microsoft\Windows\CurrentVersion\Fonts']


X11FontDirectories = [
    # an old standard installation point
    "/usr/X11R6/lib/X11/fonts/TTF/",
    "/usr/X11/lib/X11/fonts",
    # here is the new standard location for fonts
    "/usr/share/fonts/",
    # documented as a good place to install new fonts
    "/usr/local/share/fonts/",
    # common application, not really useful
    "/usr/lib/openoffice/share/fonts/truetype/",
    ]

OSXFontDirectories = [
    "/Library/Fonts/",
    "/Network/Library/Fonts/",
    "/System/Library/Fonts/",
    # fonts installed via MacPorts
    "/opt/local/share/fonts",
]

if not USE_FONTCONFIG and sys.platform != 'win32':
    OSXFontDirectories.append(str(Path.home() / "Library/Fonts"))
    X11FontDirectories.append(str(Path.home() / ".fonts"))


def get_fontext_synonyms(fontext):
    """
    Return a list of file extensions extensions that are synonyms for
    the given file extension *fileext*.
    """
    return {'ttf': ('ttf', 'otf'),
            'otf': ('ttf', 'otf'),
            'afm': ('afm',)}[fontext]


def list_fonts(directory, extensions):
    """
    Return a list of all fonts matching any of the extensions, found
    recursively under the directory.
    """
    extensions = ["." + ext for ext in extensions]
    return [str(path)
            for path in filter(Path.is_file, Path(directory).glob("**/*.*"))
            if path.suffix in extensions]


def win32FontDirectory():
    r"""
    Return the user-specified font directory for Win32.  This is
    looked up from the registry key::

      \\HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders\Fonts

    If the key is not found, $WINDIR/Fonts will be returned.
    """
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, MSFolders) as user:
            return winreg.QueryValueEx(user, 'Fonts')[0]
    except OSError:
        return os.path.join(os.environ['WINDIR'], 'Fonts')


def win32InstalledFonts(directory=None, fontext='ttf'):
    """
    Search for fonts in the specified font directory, or use the
    system directories if none given.  A list of TrueType font
    filenames are returned by default, or AFM fonts if *fontext* ==
    'afm'.
    """
    import winreg

    if directory is None:
        directory = win32FontDirectory()

    fontext = get_fontext_synonyms(fontext)

    items = set()
    for fontdir in MSFontDirectories:
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, fontdir) as local:
                for j in range(winreg.QueryInfoKey(local)[1]):
                    key, direc, tp = winreg.EnumValue(local, j)
                    if not isinstance(direc, str):
                        continue
                    # Work around for https://bugs.python.org/issue25778, which
                    # is fixed in Py>=3.6.1.
                    direc = direc.split("\0", 1)[0]
                    path = Path(directory, direc).resolve()
                    if path.suffix.lower() in fontext:
                        items.add(str(path))
        except (OSError, MemoryError):
            continue
    return list(items)


@cbook.deprecated("3.1")
def OSXInstalledFonts(directories=None, fontext='ttf'):
    """Get list of font files on OS X."""
    if directories is None:
        directories = OSXFontDirectories
    return [path
            for directory in directories
            for ext in get_fontext_synonyms(fontext)
            for path in list_fonts(directory, ext)]


@lru_cache()
def _call_fc_list():
    """Cache and list the font filenames known to `fc-list`.
    """
    # Delay the warning by 5s.
    timer = Timer(5, lambda: warnings.warn(
        'Matplotlib is building the font cache using fc-list. '
        'This may take a moment.'))
    timer.start()
    try:
        out = subprocess.check_output(['fc-list', '--format=%{file}\\n'])
    except (OSError, subprocess.CalledProcessError):
        return []
    finally:
        timer.cancel()
    return [os.fsdecode(fname) for fname in out.split(b'\n')]


def get_fontconfig_fonts(fontext='ttf'):
    """List the font filenames known to `fc-list` having the given extension.
    """
    fontext = get_fontext_synonyms(fontext)
    return [fname for fname in _call_fc_list()
            if Path(fname).suffix[1:] in fontext]


def findSystemFonts(fontpaths=None, fontext='ttf'):
    """
    Search for fonts in the specified font paths.  If no paths are
    given, will use a standard set of system paths, as well as the
    list of fonts tracked by fontconfig if fontconfig is installed and
    available.  A list of TrueType fonts are returned by default with
    AFM fonts as an option.
    """
    fontfiles = set()
    fontexts = get_fontext_synonyms(fontext)

    if fontpaths is None:
        if sys.platform == 'win32':
            fontpaths = [win32FontDirectory()]
            # now get all installed fonts directly...
            fontfiles.update(win32InstalledFonts(fontext=fontext))
        else:
            fontpaths = X11FontDirectories
            if sys.platform == 'darwin':
                fontpaths = [*X11FontDirectories, *OSXFontDirectories]
            fontfiles.update(get_fontconfig_fonts(fontext))

    elif isinstance(fontpaths, str):
        fontpaths = [fontpaths]

    for path in fontpaths:
        fontfiles.update(map(os.path.abspath, list_fonts(path, fontexts)))

    return [fname for fname in fontfiles if os.path.exists(fname)]


class FontEntry(object):
    """
    A class for storing Font properties.  It is used when populating
    the font lookup dictionary.
    """
    def __init__(self,
                 fname  ='',
                 name   ='',
                 style  ='normal',
                 variant='normal',
                 weight ='normal',
                 stretch='normal',
                 size   ='medium',
                 ):
        self.fname   = fname
        self.name    = name
        self.style   = style
        self.variant = variant
        self.weight  = weight
        self.stretch = stretch
        try:
            self.size = str(float(size))
        except ValueError:
            self.size = size

    def __repr__(self):
        return "<Font '%s' (%s) %s %s %s %s>" % (
            self.name, os.path.basename(self.fname), self.style, self.variant,
            self.weight, self.stretch)


def ttfFontProperty(font):
    """
    Extract information from a TrueType font file.

    Parameters
    ----------
    font : `.FT2Font`
        The TrueType font file from which information will be extracted.

    Returns
    -------
    `FontEntry`
        The extracted font properties.

    """
    name = font.family_name

    #  Styles are: italic, oblique, and normal (default)

    sfnt = font.get_sfnt()
    # These tables are actually mac_roman-encoded, but mac_roman support may be
    # missing in some alternative Python implementations and we are only going
    # to look for ASCII substrings, where any ASCII-compatible encoding works.
    sfnt2 = sfnt.get((1, 0, 0, 2), b'').decode('latin-1').lower()
    sfnt4 = sfnt.get((1, 0, 0, 4), b'').decode('latin-1').lower()
    if sfnt4.find('oblique') >= 0:
        style = 'oblique'
    elif sfnt4.find('italic') >= 0:
        style = 'italic'
    elif sfnt2.find('regular') >= 0:
        style = 'normal'
    elif font.style_flags & ft2font.ITALIC:
        style = 'italic'
    else:
        style = 'normal'

    #  Variants are: small-caps and normal (default)

    #  !!!!  Untested
    if name.lower() in ['capitals', 'small-caps']:
        variant = 'small-caps'
    else:
        variant = 'normal'

    weight = next((w for w in weight_dict if sfnt4.find(w) >= 0), None)
    if not weight:
        if font.style_flags & ft2font.BOLD:
            weight = 700
        else:
            weight = 400

    #  Stretch can be absolute and relative
    #  Absolute stretches are: ultra-condensed, extra-condensed, condensed,
    #    semi-condensed, normal, semi-expanded, expanded, extra-expanded,
    #    and ultra-expanded.
    #  Relative stretches are: wider, narrower
    #  Child value is: inherit

    if (sfnt4.find('narrow') >= 0 or sfnt4.find('condensed') >= 0 or
            sfnt4.find('cond') >= 0):
        stretch = 'condensed'
    elif sfnt4.find('demi cond') >= 0:
        stretch = 'semi-condensed'
    elif sfnt4.find('wide') >= 0 or sfnt4.find('expanded') >= 0:
        stretch = 'expanded'
    else:
        stretch = 'normal'

    #  Sizes can be absolute and relative.
    #  Absolute sizes are: xx-small, x-small, small, medium, large, x-large,
    #    and xx-large.
    #  Relative sizes are: larger, smaller
    #  Length value is an absolute font size, e.g., 12pt
    #  Percentage values are in 'em's.  Most robust specification.

    if not font.scalable:
        raise NotImplementedError("Non-scalable fonts are not supported")
    size = 'scalable'

    return FontEntry(font.fname, name, style, variant, weight, stretch, size)


def afmFontProperty(fontpath, font):
    """
    Extract information from an AFM font file.

    Parameters
    ----------
    font : `.AFM`
        The AFM font file from which information will be extracted.

    Returns
    -------
    `FontEntry`
        The extracted font properties.

    """

    name = font.get_familyname()
    fontname = font.get_fontname().lower()

    #  Styles are: italic, oblique, and normal (default)

    if font.get_angle() != 0 or 'italic' in name.lower():
        style = 'italic'
    elif 'oblique' in name.lower():
        style = 'oblique'
    else:
        style = 'normal'

    #  Variants are: small-caps and normal (default)

    # !!!!  Untested
    if name.lower() in ['capitals', 'small-caps']:
        variant = 'small-caps'
    else:
        variant = 'normal'

    weight = font.get_weight().lower()

    #  Stretch can be absolute and relative
    #  Absolute stretches are: ultra-condensed, extra-condensed, condensed,
    #    semi-condensed, normal, semi-expanded, expanded, extra-expanded,
    #    and ultra-expanded.
    #  Relative stretches are: wider, narrower
    #  Child value is: inherit
    if 'demi cond' in fontname:
        stretch = 'semi-condensed'
    elif 'narrow' in fontname or 'cond' in fontname:
        stretch = 'condensed'
    elif 'wide' in fontname or 'expanded' in fontname:
        stretch = 'expanded'
    else:
        stretch = 'normal'

    #  Sizes can be absolute and relative.
    #  Absolute sizes are: xx-small, x-small, small, medium, large, x-large,
    #    and xx-large.
    #  Relative sizes are: larger, smaller
    #  Length value is an absolute font size, e.g., 12pt
    #  Percentage values are in 'em's.  Most robust specification.

    #  All AFM fonts are apparently scalable.

    size = 'scalable'

    return FontEntry(fontpath, name, style, variant, weight, stretch, size)


def createFontList(fontfiles, fontext='ttf'):
    """
    A function to create a font lookup list.  The default is to create
    a list of TrueType fonts.  An AFM font list can optionally be
    created.
    """

    fontlist = []
    #  Add fonts from list of known font files.
    seen = set()
    for fpath in fontfiles:
        _log.debug('createFontDict: %s', fpath)
        fname = os.path.split(fpath)[1]
        if fname in seen:
            continue
        else:
            seen.add(fname)
        if fontext == 'afm':
            try:
                with open(fpath, 'rb') as fh:
                    font = afm.AFM(fh)
            except EnvironmentError:
                _log.info("Could not open font file %s", fpath)
                continue
            except RuntimeError:
                _log.info("Could not parse font file %s", fpath)
                continue
            try:
                prop = afmFontProperty(fpath, font)
            except KeyError:
                continue
        else:
            try:
                font = ft2font.FT2Font(fpath)
            except RuntimeError:
                _log.info("Could not open font file %s", fpath)
                continue
            except UnicodeError:
                _log.info("Cannot handle unicode filenames")
                continue
            except OSError:
                _log.info("IO error - cannot open font file %s", fpath)
                continue
            try:
                prop = ttfFontProperty(font)
            except (KeyError, RuntimeError, ValueError, NotImplementedError):
                continue

        fontlist.append(prop)
    return fontlist


class FontProperties(object):
    """
    A class for storing and manipulating font properties.

    The font properties are those described in the `W3C Cascading
    Style Sheet, Level 1
    <http://www.w3.org/TR/1998/REC-CSS2-19980512/>`_ font
    specification.  The six properties are:

      - family: A list of font names in decreasing order of priority.
        The items may include a generic font family name, either
        'serif', 'sans-serif', 'cursive', 'fantasy', or 'monospace'.
        In that case, the actual font to be used will be looked up
        from the associated rcParam.

      - style: Either 'normal', 'italic' or 'oblique'.

      - variant: Either 'normal' or 'small-caps'.

      - stretch: A numeric value in the range 0-1000 or one of
        'ultra-condensed', 'extra-condensed', 'condensed',
        'semi-condensed', 'normal', 'semi-expanded', 'expanded',
        'extra-expanded' or 'ultra-expanded'

      - weight: A numeric value in the range 0-1000 or one of
        'ultralight', 'light', 'normal', 'regular', 'book', 'medium',
        'roman', 'semibold', 'demibold', 'demi', 'bold', 'heavy',
        'extra bold', 'black'

      - size: Either an relative value of 'xx-small', 'x-small',
        'small', 'medium', 'large', 'x-large', 'xx-large' or an
        absolute font size, e.g., 12

    The default font property for TrueType fonts (as specified in the
    default rcParams) is::

      sans-serif, normal, normal, normal, normal, scalable.

    Alternatively, a font may be specified using an absolute path to a
    .ttf file, by using the *fname* kwarg.

    The preferred usage of font sizes is to use the relative values,
    e.g.,  'large', instead of absolute font sizes, e.g., 12.  This
    approach allows all text sizes to be made larger or smaller based
    on the font manager's default font size.

    This class will also accept a `fontconfig
    <https://www.freedesktop.org/wiki/Software/fontconfig/>`_ pattern, if it is
    the only argument provided.  See the documentation on `fontconfig patterns
    <https://www.freedesktop.org/software/fontconfig/fontconfig-user.html>`_.
    This support does not require fontconfig to be installed.  We are merely
    borrowing its pattern syntax for use here.

    Note that Matplotlib's internal font manager and fontconfig use a
    different algorithm to lookup fonts, so the results of the same pattern
    may be different in Matplotlib than in other applications that use
    fontconfig.
    """

    def __init__(self,
                 family = None,
                 style  = None,
                 variant= None,
                 weight = None,
                 stretch= None,
                 size   = None,
                 fname  = None,  # if set, it's a hardcoded filename to use
                 _init  = None,  # used only by copy()
                 ):
        self._family = _normalize_font_family(rcParams['font.family'])
        self._slant = rcParams['font.style']
        self._variant = rcParams['font.variant']
        self._weight = rcParams['font.weight']
        self._stretch = rcParams['font.stretch']
        self._size = rcParams['font.size']
        self._file = None

        # This is used only by copy()
        if _init is not None:
            self.__dict__.update(_init.__dict__)
            return

        if isinstance(family, str):
            # Treat family as a fontconfig pattern if it is the only
            # parameter provided.
            if (style is None and
                variant is None and
                weight is None and
                stretch is None and
                size is None and
                fname is None):
                self.set_fontconfig_pattern(family)
                return

        self.set_family(family)
        self.set_style(style)
        self.set_variant(variant)
        self.set_weight(weight)
        self.set_stretch(stretch)
        self.set_file(fname)
        self.set_size(size)

    def _parse_fontconfig_pattern(self, pattern):
        return parse_fontconfig_pattern(pattern)

    def __hash__(self):
        l = (tuple(self.get_family()),
             self.get_slant(),
             self.get_variant(),
             self.get_weight(),
             self.get_stretch(),
             self.get_size_in_points(),
             self.get_file())
        return hash(l)

    def __eq__(self, other):
        return hash(self) == hash(other)

    def __str__(self):
        return self.get_fontconfig_pattern()

    def get_family(self):
        """
        Return a list of font names that comprise the font family.
        """
        return self._family

    def get_name(self):
        """
        Return the name of the font that best matches the font properties.
        """
        return get_font(findfont(self)).family_name

    def get_style(self):
        """
        Return the font style.  Values are: 'normal', 'italic' or 'oblique'.
        """
        return self._slant
    get_slant = get_style

    def get_variant(self):
        """
        Return the font variant.  Values are: 'normal' or 'small-caps'.
        """
        return self._variant

    def get_weight(self):
        """
        Set the font weight.  Options are: A numeric value in the
        range 0-1000 or one of 'light', 'normal', 'regular', 'book',
        'medium', 'roman', 'semibold', 'demibold', 'demi', 'bold',
        'heavy', 'extra bold', 'black'
        """
        return self._weight

    def get_stretch(self):
        """
        Return the font stretch or width.  Options are: 'ultra-condensed',
        'extra-condensed', 'condensed', 'semi-condensed', 'normal',
        'semi-expanded', 'expanded', 'extra-expanded', 'ultra-expanded'.
        """
        return self._stretch

    def get_size(self):
        """
        Return the font size.
        """
        return self._size

    def get_size_in_points(self):
        return self._size

    def get_file(self):
        """
        Return the filename of the associated font.
        """
        return self._file

    def get_fontconfig_pattern(self):
        """
        Get a fontconfig pattern suitable for looking up the font as
        specified with fontconfig's ``fc-match`` utility.

        See the documentation on `fontconfig patterns
        <https://www.freedesktop.org/software/fontconfig/fontconfig-user.html>`_.

        This support does not require fontconfig to be installed or
        support for it to be enabled.  We are merely borrowing its
        pattern syntax for use here.
        """
        return generate_fontconfig_pattern(self)

    def set_family(self, family):
        """
        Change the font family.  May be either an alias (generic name
        is CSS parlance), such as: 'serif', 'sans-serif', 'cursive',
        'fantasy', or 'monospace', a real font name or a list of real
        font names.  Real font names are not supported when
        `text.usetex` is `True`.
        """
        if family is None:
            family = rcParams['font.family']
        self._family = _normalize_font_family(family)
    set_name = set_family

    def set_style(self, style):
        """
        Set the font style.  Values are: 'normal', 'italic' or 'oblique'.
        """
        if style is None:
            style = rcParams['font.style']
        if style not in ('normal', 'italic', 'oblique'):
            raise ValueError("style must be normal, italic or oblique")
        self._slant = style
    set_slant = set_style

    def set_variant(self, variant):
        """
        Set the font variant.  Values are: 'normal' or 'small-caps'.
        """
        if variant is None:
            variant = rcParams['font.variant']
        if variant not in ('normal', 'small-caps'):
            raise ValueError("variant must be normal or small-caps")
        self._variant = variant

    def set_weight(self, weight):
        """
        Set the font weight.  May be either a numeric value in the
        range 0-1000 or one of 'ultralight', 'light', 'normal',
        'regular', 'book', 'medium', 'roman', 'semibold', 'demibold',
        'demi', 'bold', 'heavy', 'extra bold', 'black'
        """
        if weight is None:
            weight = rcParams['font.weight']
        try:
            weight = int(weight)
            if weight < 0 or weight > 1000:
                raise ValueError()
        except ValueError:
            if weight not in weight_dict:
                raise ValueError("weight is invalid")
        self._weight = weight

    def set_stretch(self, stretch):
        """
        Set the font stretch or width.  Options are: 'ultra-condensed',
        'extra-condensed', 'condensed', 'semi-condensed', 'normal',
        'semi-expanded', 'expanded', 'extra-expanded' or
        'ultra-expanded', or a numeric value in the range 0-1000.
        """
        if stretch is None:
            stretch = rcParams['font.stretch']
        try:
            stretch = int(stretch)
            if stretch < 0 or stretch > 1000:
                raise ValueError()
        except ValueError:
            if stretch not in stretch_dict:
                raise ValueError("stretch is invalid")
        self._stretch = stretch

    def set_size(self, size):
        """
        Set the font size.  Either an relative value of 'xx-small',
        'x-small', 'small', 'medium', 'large', 'x-large', 'xx-large'
        or an absolute font size, e.g., 12.
        """
        if size is None:
            size = rcParams['font.size']
        try:
            size = float(size)
        except ValueError:
            try:
                scale = font_scalings[size]
            except KeyError:
                raise ValueError(
                    "Size is invalid. Valid font size are "
                    + ", ".join(map(str, font_scalings)))
            else:
                size = scale * FontManager.get_default_size()
        if size < 1.0:
            _log.info('Fontsize %1.2f < 1.0 pt not allowed by FreeType. '
                      'Setting fontsize = 1 pt', size)
            size = 1.0
        self._size = size

    def set_file(self, file):
        """
        Set the filename of the fontfile to use.  In this case, all
        other properties will be ignored.
        """
        self._file = file

    def set_fontconfig_pattern(self, pattern):
        """
        Set the properties by parsing a fontconfig *pattern*.

        See the documentation on `fontconfig patterns
        <https://www.freedesktop.org/software/fontconfig/fontconfig-user.html>`_.

        This support does not require fontconfig to be installed or
        support for it to be enabled.  We are merely borrowing its
        pattern syntax for use here.
        """
        for key, val in self._parse_fontconfig_pattern(pattern).items():
            if type(val) == list:
                getattr(self, "set_" + key)(val[0])
            else:
                getattr(self, "set_" + key)(val)

    def copy(self):
        """Return a deep copy of self"""
        return FontProperties(_init=self)


class JSONEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, FontManager):
            return dict(o.__dict__, __class__='FontManager')
        elif isinstance(o, FontEntry):
            d = dict(o.__dict__, __class__='FontEntry')
            try:
                # Cache paths of fonts shipped with mpl relative to the mpl
                # data path, which helps in the presence of venvs.
                d["fname"] = str(
                    Path(d["fname"]).relative_to(mpl.get_data_path()))
            except ValueError:
                pass
            return d
        else:
            return super().default(o)


def _json_decode(o):
    cls = o.pop('__class__', None)
    if cls is None:
        return o
    elif cls == 'FontManager':
        r = FontManager.__new__(FontManager)
        r.__dict__.update(o)
        return r
    elif cls == 'FontEntry':
        r = FontEntry.__new__(FontEntry)
        r.__dict__.update(o)
        if not os.path.isabs(r.fname):
            r.fname = os.path.join(mpl.get_data_path(), r.fname)
        return r
    else:
        raise ValueError("don't know how to deserialize __class__=%s" % cls)


def json_dump(data, filename):
    """
    Dumps a data structure as JSON in the named file.

    Handles FontManager and its fields.  File paths that are children of the
    Matplotlib data path (typically, fonts shipped with Matplotlib) are stored
    relative to that data path (to remain valid across virtualenvs).
    """
    with open(filename, 'w') as fh:
        try:
            json.dump(data, fh, cls=JSONEncoder, indent=2)
        except OSError as e:
            warnings.warn('Could not save font_manager cache {}'.format(e))


def json_load(filename):
    """
    Loads a data structure as JSON from the named file.

    Handles FontManager and its fields.  Relative file paths are interpreted
    as being relative to the Matplotlib data path, and transformed into
    absolute paths.
    """
    with open(filename, 'r') as fh:
        return json.load(fh, object_hook=_json_decode)


def _normalize_font_family(family):
    if isinstance(family, str):
        family = [family]
    return family


@cbook.deprecated("3.0")
class TempCache(object):
    """
    A class to store temporary caches that are (a) not saved to disk
    and (b) invalidated whenever certain font-related
    rcParams---namely the family lookup lists---are changed or the
    font cache is reloaded.  This avoids the expensive linear search
    through all fonts every time a font is looked up.
    """
    # A list of rcparam names that, when changed, invalidated this
    # cache.
    invalidating_rcparams = (
        'font.serif', 'font.sans-serif', 'font.cursive', 'font.fantasy',
        'font.monospace')

    def __init__(self):
        self._lookup_cache = {}
        self._last_rcParams = self.make_rcparams_key()

    def make_rcparams_key(self):
        return [id(fontManager)] + [
            rcParams[param] for param in self.invalidating_rcparams]

    def get(self, prop):
        key = self.make_rcparams_key()
        if key != self._last_rcParams:
            self._lookup_cache = {}
            self._last_rcParams = key
        return self._lookup_cache.get(prop)

    def set(self, prop, value):
        key = self.make_rcparams_key()
        if key != self._last_rcParams:
            self._lookup_cache = {}
            self._last_rcParams = key
        self._lookup_cache[prop] = value


class FontManager(object):
    """
    On import, the :class:`FontManager` singleton instance creates a
    list of TrueType fonts based on the font properties: name, style,
    variant, weight, stretch, and size.  The :meth:`findfont` method
    does a nearest neighbor search to find the font that most closely
    matches the specification.  If no good enough match is found, a
    default font is returned.
    """
    # Increment this version number whenever the font cache data
    # format or behavior has changed and requires a existing font
    # cache files to be rebuilt.
    __version__ = 300

    def __init__(self, size=None, weight='normal'):
        self._version = self.__version__

        self.__default_weight = weight
        self.default_size = size

        paths = [os.path.join(rcParams['datapath'], 'fonts', 'ttf'),
                 os.path.join(rcParams['datapath'], 'fonts', 'afm'),
                 os.path.join(rcParams['datapath'], 'fonts', 'pdfcorefonts')]

        #  Create list of font paths
        for pathname in ['TTFPATH', 'AFMPATH']:
            if pathname in os.environ:
                ttfpath = os.environ[pathname]
                if ttfpath.find(';') >= 0:  # win32 style
                    paths.extend(ttfpath.split(';'))
                elif ttfpath.find(':') >= 0:  # unix style
                    paths.extend(ttfpath.split(':'))
                else:
                    paths.append(ttfpath)
        _log.debug('font search path %s', str(paths))
        #  Load TrueType fonts and create font dictionary.

        self.defaultFamily = {
            'ttf': 'DejaVu Sans',
            'afm': 'Helvetica'}
        self.defaultFont = {}

        ttffiles = findSystemFonts(paths) + findSystemFonts()
        self.defaultFont['ttf'] = next(
            (fname for fname in ttffiles
             if fname.lower().endswith("dejavusans.ttf")),
            ttffiles[0])
        self.ttflist = createFontList(ttffiles)

        afmfiles = (findSystemFonts(paths, fontext='afm')
                    + findSystemFonts(fontext='afm'))
        self.afmlist = createFontList(afmfiles, fontext='afm')
        self.defaultFont['afm'] = afmfiles[0] if afmfiles else None

    @property
    @cbook.deprecated("3.0")
    def ttffiles(self):
        return [font.fname for font in self.ttflist]

    @property
    @cbook.deprecated("3.0")
    def afmfiles(self):
        return [font.fname for font in self.afmlist]

    def get_default_weight(self):
        """
        Return the default font weight.
        """
        return self.__default_weight

    @staticmethod
    def get_default_size():
        """
        Return the default font size.
        """
        return rcParams['font.size']

    def set_default_weight(self, weight):
        """
        Set the default font weight.  The initial value is 'normal'.
        """
        self.__default_weight = weight

    def update_fonts(self, filenames):
        """
        Update the font dictionary with new font files.
        Currently not implemented.
        """
        #  !!!!  Needs implementing
        raise NotImplementedError

    # Each of the scoring functions below should return a value between
    # 0.0 (perfect match) and 1.0 (terrible match)
    def score_family(self, families, family2):
        """
        Returns a match score between the list of font families in
        *families* and the font family name *family2*.

        An exact match at the head of the list returns 0.0.

        A match further down the list will return between 0 and 1.

        No match will return 1.0.
        """
        if not isinstance(families, (list, tuple)):
            families = [families]
        elif len(families) == 0:
            return 1.0
        family2 = family2.lower()
        step = 1 / len(families)
        for i, family1 in enumerate(families):
            family1 = family1.lower()
            if family1 in font_family_aliases:
                if family1 in ('sans', 'sans serif'):
                    family1 = 'sans-serif'
                options = rcParams['font.' + family1]
                options = [x.lower() for x in options]
                if family2 in options:
                    idx = options.index(family2)
                    return (i + (idx / len(options))) * step
            elif family1 == family2:
                # The score should be weighted by where in the
                # list the font was found.
                return i * step
        return 1.0

    def score_style(self, style1, style2):
        """
        Returns a match score between *style1* and *style2*.

        An exact match returns 0.0.

        A match between 'italic' and 'oblique' returns 0.1.

        No match returns 1.0.
        """
        if style1 == style2:
            return 0.0
        elif style1 in ('italic', 'oblique') and \
                style2 in ('italic', 'oblique'):
            return 0.1
        return 1.0

    def score_variant(self, variant1, variant2):
        """
        Returns a match score between *variant1* and *variant2*.

        An exact match returns 0.0, otherwise 1.0.
        """
        if variant1 == variant2:
            return 0.0
        else:
            return 1.0

    def score_stretch(self, stretch1, stretch2):
        """
        Returns a match score between *stretch1* and *stretch2*.

        The result is the absolute value of the difference between the
        CSS numeric values of *stretch1* and *stretch2*, normalized
        between 0.0 and 1.0.
        """
        try:
            stretchval1 = int(stretch1)
        except ValueError:
            stretchval1 = stretch_dict.get(stretch1, 500)
        try:
            stretchval2 = int(stretch2)
        except ValueError:
            stretchval2 = stretch_dict.get(stretch2, 500)
        return abs(stretchval1 - stretchval2) / 1000.0

    def score_weight(self, weight1, weight2):
        """
        Returns a match score between *weight1* and *weight2*.

        The result is 0.0 if both weight1 and weight 2 are given as strings
        and have the same value.

        Otherwise, the result is the absolute value of the difference between
        the CSS numeric values of *weight1* and *weight2*, normalized between
        0.05 and 1.0.
        """

        # exact match of the weight names, e.g. weight1 == weight2 == "regular"
        if (isinstance(weight1, str) and
                isinstance(weight2, str) and
                weight1 == weight2):
            return 0.0
        try:
            weightval1 = int(weight1)
        except ValueError:
            weightval1 = weight_dict.get(weight1, 500)
        try:
            weightval2 = int(weight2)
        except ValueError:
            weightval2 = weight_dict.get(weight2, 500)
        return 0.95*(abs(weightval1 - weightval2) / 1000.0) + 0.05

    def score_size(self, size1, size2):
        """
        Returns a match score between *size1* and *size2*.

        If *size2* (the size specified in the font file) is 'scalable', this
        function always returns 0.0, since any font size can be generated.

        Otherwise, the result is the absolute distance between *size1* and
        *size2*, normalized so that the usual range of font sizes (6pt -
        72pt) will lie between 0.0 and 1.0.
        """
        if size2 == 'scalable':
            return 0.0
        # Size value should have already been
        try:
            sizeval1 = float(size1)
        except ValueError:
            sizeval1 = self.default_size * font_scalings[size1]
        try:
            sizeval2 = float(size2)
        except ValueError:
            return 1.0
        return abs(sizeval1 - sizeval2) / 72.0

    def findfont(self, prop, fontext='ttf', directory=None,
                 fallback_to_default=True, rebuild_if_missing=True):
        """
        Search the font list for the font that most closely matches
        the :class:`FontProperties` *prop*.

        :meth:`findfont` performs a nearest neighbor search.  Each
        font is given a similarity score to the target font
        properties.  The first font with the highest score is
        returned.  If no matches below a certain threshold are found,
        the default font (usually DejaVu Sans) is returned.

        `directory`, is specified, will only return fonts from the
        given directory (or subdirectory of that directory).

        The result is cached, so subsequent lookups don't have to
        perform the O(n) nearest neighbor search.

        If `fallback_to_default` is True, will fallback to the default
        font family (usually "DejaVu Sans" or "Helvetica") if
        the first lookup hard-fails.

        See the `W3C Cascading Style Sheet, Level 1
        <http://www.w3.org/TR/1998/REC-CSS2-19980512/>`_ documentation
        for a description of the font finding algorithm.
        """
        # Pass the relevant rcParams (and the font manager, as `self`) to
        # _findfont_cached so to prevent using a stale cache entry after an
        # rcParam was changed.
        rc_params = tuple(tuple(rcParams[key]) for key in [
            "font.serif", "font.sans-serif", "font.cursive", "font.fantasy",
            "font.monospace"])
        return self._findfont_cached(
            prop, fontext, directory, fallback_to_default, rebuild_if_missing,
            rc_params)

    @lru_cache()
    def _findfont_cached(self, prop, fontext, directory, fallback_to_default,
                         rebuild_if_missing, rc_params):

        if not isinstance(prop, FontProperties):
            prop = FontProperties(prop)
        fname = prop.get_file()

        if fname is not None:
            _log.debug('findfont returning %s', fname)
            return fname

        if fontext == 'afm':
            fontlist = self.afmlist
        else:
            fontlist = self.ttflist

        best_score = 1e64
        best_font = None

        for font in fontlist:
            if (directory is not None and
                    Path(directory) not in Path(font.fname).parents):
                continue
            # Matching family should have highest priority, so it is multiplied
            # by 10.0
            score = \
                self.score_family(prop.get_family(), font.name) * 10.0 + \
                self.score_style(prop.get_style(), font.style) + \
                self.score_variant(prop.get_variant(), font.variant) + \
                self.score_weight(prop.get_weight(), font.weight) + \
                self.score_stretch(prop.get_stretch(), font.stretch) + \
                self.score_size(prop.get_size(), font.size)
            if score < best_score:
                best_score = score
                best_font = font
            if score == 0:
                break

        if best_font is None or best_score >= 10.0:
            if fallback_to_default:
                warnings.warn(
                    'findfont: Font family %s not found. Falling back to %s.' %
                    (prop.get_family(), self.defaultFamily[fontext]))
                default_prop = prop.copy()
                default_prop.set_family(self.defaultFamily[fontext])
                return self.findfont(default_prop, fontext, directory, False)
            else:
                # This is a hard fail -- we can't find anything reasonable,
                # so just return the DejuVuSans.ttf
                warnings.warn('findfont: Could not match %s. Returning %s.' %
                              (prop, self.defaultFont[fontext]),
                              UserWarning)
                result = self.defaultFont[fontext]
        else:
            _log.debug('findfont: Matching %s to %s (%r) with score of %f.',
                       prop, best_font.name, best_font.fname, best_score)
            result = best_font.fname

        if not os.path.isfile(result):
            if rebuild_if_missing:
                _log.info(
                    'findfont: Found a missing font file.  Rebuilding cache.')
                _rebuild()
                return fontManager.findfont(
                    prop, fontext, directory, True, False)
            else:
                raise ValueError("No valid font could be found")

        return result


@lru_cache()
def is_opentype_cff_font(filename):
    """
    Returns True if the given font is a Postscript Compact Font Format
    Font embedded in an OpenType wrapper.  Used by the PostScript and
    PDF backends that can not subset these fonts.
    """
    if os.path.splitext(filename)[1].lower() == '.otf':
        with open(filename, 'rb') as fd:
            return fd.read(4) == b"OTTO"
    else:
        return False


fontManager = None
_fmcache = None


_get_font = lru_cache(64)(ft2font.FT2Font)


def get_font(filename, hinting_factor=None):
    if hinting_factor is None:
        hinting_factor = rcParams['text.hinting_factor']
    return _get_font(filename, hinting_factor)


# The experimental fontconfig-based backend.
if USE_FONTCONFIG and sys.platform != 'win32':

    def fc_match(pattern, fontext):
        fontexts = get_fontext_synonyms(fontext)
        ext = "." + fontext
        try:
            pipe = subprocess.Popen(
                ['fc-match', '-s', '--format=%{file}\\n', pattern],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE)
            output = pipe.communicate()[0]
        except OSError:
            return None

        # The bulk of the output from fc-list is ascii, so we keep the
        # result in bytes and parse it as bytes, until we extract the
        # filename, which is in sys.filesystemencoding().
        if pipe.returncode == 0:
            for fname in map(os.fsdecode, output.split(b'\n')):
                if os.path.splitext(fname)[1][1:] in fontexts:
                    return fname
        return None

    _fc_match_cache = {}

    def findfont(prop, fontext='ttf'):
        if not isinstance(prop, str):
            prop = prop.get_fontconfig_pattern()
        cached = _fc_match_cache.get(prop)
        if cached is not None:
            return cached

        result = fc_match(prop, fontext)
        if result is None:
            result = fc_match(':', fontext)

        _fc_match_cache[prop] = result
        return result

else:
    _fmcache = None

    cachedir = get_cachedir()
    if cachedir is not None:
        _fmcache = os.path.join(
            cachedir, 'fontlist-v{}.json'.format(FontManager.__version__))

    fontManager = None

    def _rebuild():
        global fontManager

        fontManager = FontManager()

        if _fmcache:
            with cbook._lock_path(_fmcache):
                json_dump(fontManager, _fmcache)
        _log.debug("generated new fontManager")

    if _fmcache:
        try:
            fontManager = json_load(_fmcache)
            if (not hasattr(fontManager, '_version') or
                fontManager._version != FontManager.__version__):
                _rebuild()
            else:
                fontManager.default_size = None
                _log.debug("Using fontManager instance from %s", _fmcache)
        except TimeoutError:
            raise
        except Exception:
            _rebuild()
    else:
        _rebuild()

    def findfont(prop, **kw):
        global fontManager
        font = fontManager.findfont(prop, **kw)
        return font
