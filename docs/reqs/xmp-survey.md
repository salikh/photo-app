# XMP sidecar survey of /zoo/Pictures

Measured 2026-09-21 (read-only pass over all `*.xmp`, any case).

| Fact | Count |
|---|---|
| Sidecars in total | 15429 |
| `NAME.JPG.xmp` | 7821 |
| `NAME.DNG.xmp` | 7602 |
| `NAME.PNG.xmp` / `NAME.PEF.xmp` | 3 / 1 |
| bare `NAME.xmp` / `NAME.XMP` (RAW style) | 1 / 1 |
| Written by darktable (`darktable:` namespace) | 15427 |
| `xmp:Rating` as an attribute on `rdf:Description` | 15427 |
| `xap:Rating` as a child element (older Adobe) | 1 |
| Files with `Rating="-1"` (reject) | 565 |
| With `dc:subject` | 67 |
| With `lr:hierarchicalSubject` | 66 |
| With `<?xpacket` wrapper or `crs:` develop data | 1 |
| No Rating at all | 1 |

## What this changes

- The requirements assume Lightroom wrote most sidecars and that `NAME.xmp`
  is the RAW convention. In practice the library is almost entirely
  **darktable, full-filename style for both DNG and JPG** (`K___2177.DNG.xmp`).
  Bare `NAME.xmp` is a rarity.
- Consequently a DNG and its JPG almost always have **separate sidecars**
  (`X.DNG.xmp` and `X.JPG.xmp`), so the conflict survey (ticket 023) matters.
- Lossless editing (tickets 019, 020) should be tested mainly against
  darktable output (many `darktable:*` attributes, `rdf:Seq` history), with
  one Lightroom-style sample for the `xpacket`/`crs:` case.
- New-sidecar naming for RAW originals needs a decision: ticket 042.
