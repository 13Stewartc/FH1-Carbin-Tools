FH1 CARBIN Tools v0.1.0 — Blender 3.6

Install the ZIP through Edit > Preferences > Add-ons > Install.
Enable "FH1 CARBIN Tools" (Import-Export category).
Use File > Import > Forza Horizon 1 CARBIN (.carbin).

File type: Auto (by filename), LOD1–LOD5, or LOD0 / Cockpit.
Auto selects LOD0/Cockpit for names ending _lod0 or _cockpit; otherwise LOD1–5.
For renamed files without those suffixes, select the correct type manually.
LOD1–5 supports independent LOD1..LOD5 checkboxes.
Topology diagnostics are optional and appear in Blender's system console.

Parser engines are the user's tested v0.8.4 and v0.15.3, unchanged.
No exporter yet. Source files are never modified.

Caveat: existing standalone importers use filename suffix to distinguish
cockpit from exterior LOD0. A renamed cockpit file is treated as exterior.

Version 0.1.1: supports CARBIN header version 1, 16-bit index sections for brake rotor/caliper LOD0 files. Version 5 LOD0/cockpit remains 32-bit. Brake parts are imported under a Brake Parts collection.

Version 0.1.2: Auto detects version-1 parts by binary header; console reports loaded add-on version. Remove old installed add-on and restart Blender before installing.

Version 0.1.4: defer Outliner folding until after file browser closes.

v0.1.5 EXPERIMENTAL XDS COMPANION
- New separate xds_textures.py module. Import dialog checkbox loads image files
  already exported from XDS by Forza Studio (PNG/TGA/BMP/TIFF/DDS) from the
  same folder as the selected CARBIN. It does NOT decode raw XDS or automatically
  wire material nodes; the material-to-atlas mapping remains unverified.
- Decompiled Forza Studio 4.6 n.cs invokes native xds.dll exports Create,
  GetTextureSize and ConvertToBitmap. Its managed source alone is not a usable
  decoder. No native xds.dll was included in the supplied source archive.
- The checkbox is off by default; tested CARBIN import paths remain unchanged.
