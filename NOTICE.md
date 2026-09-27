# Third-party notices

This project (Apache License 2.0, see LICENSE) vendors and depends on third-party code
with its own license terms.

## libraw-wasm (vendored)

`photoapp/static/vendor/libraw-wasm/` contains a copy of
[LibRaw-Wasm](https://github.com/ybouane/LibRaw-Wasm) 1.6.0, checked into this repository
because this machine has no npm access (ticket 102). Its `package.json` declares an ISC
license; upstream ships no LICENSE file, so the
standard ISC text is reproduced at
`photoapp/static/vendor/libraw-wasm/LICENSE`, satisfying that license's requirement to
keep the copyright and permission notice with any copy.

## LibRaw (compiled into libraw.wasm)

`libraw.wasm` in that same directory is a WebAssembly build of the
[LibRaw](https://www.libraw.org) C++ library, compiled by the LibRaw-Wasm project. LibRaw
is dual-licensed under the **GNU Lesser General Public License v2.1** and the **CDDL
v1.0** — not the ISC terms above, and not mentioned anywhere in the LibRaw-Wasm repo. The
LGPL 2.1 text is reproduced at
[`third_party_licenses/LGPL-2.1-LibRaw.txt`](third_party_licenses/LGPL-2.1-LibRaw.txt)
(copied from the `rawpy` package below, which bundles the same LibRaw license for the
same reason). CDDL v1.0 is available from
<https://opensource.org/license/cddl-1-0>.

This project also uses LibRaw server-side via the `rawpy` Python package (MIT-licensed,
installed from PyPI, not vendored into this repo) for the same reason: Pillow can't
decode RAW files correctly (see the photo-manager project notes). `rawpy` bundles its own
copy of the LibRaw LGPL license in its distribution; no separate action is needed here
since it is a regular pip dependency, not code copied into this repository.
