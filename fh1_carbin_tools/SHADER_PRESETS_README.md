# FH1 shader presets — Blender 3.6.x

This file documents `shader_presets.json`, which configures **Blender preview appearance only**. It does not modify the original FH1 shader metadata or write material records into `.carbin`.

## Where to edit

Open `shader_presets.json` inside the installed `fh1_carbin_tools` addon folder. Restart Blender (or reload the addon) after changes. The JSON must remain valid (double-quoted keys/strings, no trailing commas, no comments).

`shaders` contains entries keyed by the **FH1 shader or family name**, not by the numbered Blender name. For example, `misc` applies to `FH1_misc`, `FH1_misc2`, etc. Family defaults are applied first, then shader-specific entries when present.

## All Principled fields are listed

Each `principled` object lists the Blender **3.6** Principled BSDF socket labels. `null` means **do not override**. The addon creates/uses the existing Principled node and leaves unconfigured inputs at their existing Blender defaults. A linked socket is not overridden. Blender-only visual fixes such as alpha remain separate from these presets.

Example:

```json
{
  "version": 1,
  "shaders": {
    "misc": {
      "principled": {
        "Base Color": null,
        "Metallic": null,
        "Roughness": 0.45,
        "Specular": 0.25
      },
      "preview_overrides": {}
    }
  }
}
```

The distributed JSON includes the full set of 3.6 fields for each included shader entry, so you don't need to memorise names. Fields like `Normal`, `Clearcoat Normal`, and `Tangent` are connection inputs; leave these `null` until dedicated node-link configuration is implemented.

## Colour formats

For colour inputs such as `Base Color`:

```json
"Base Color": {"hex": "#228B22"}
"Base Color": {"hex": "#228B22FF"}
"Base Color": {"rgb": [34, 139, 34]}
"Base Color": {"rgba": [34, 139, 34, 255]}
"Base Color": {"hsv": [120, 0.76, 0.55]}
```

Use **one** representation per field. RGB/RGBA channels are 0–255. HSV uses hue 0–360 degrees, saturation/value 0–1. HEX/RGB/HSV are treated as display-space sRGB and converted to Blender's linear colour values; alpha is not gamma-corrected.

`null` for Base Color means the existing FH1 diffuse sampler resolver should continue to connect its automatically located texture; otherwise Blender's native default remains. A configured colour is applied only if Base Color has no texture link.

## Textures and future fields

The following is the **planned** format, **not yet implemented in this build**:

```json
"Base Color": {"texture": "fh1-plate"}
"Base Color": {"texture": "fh1-plate", "tint": {"hex": "#FFFFFF"}}
```

The current preset loader detects `texture` but deliberately does **not** create a node for it. The existing material resolver still loads textures from `ShaderSettings/Normal.xml`, `textures.xml`, the car folder and shared directory. Custom XDS file picking, user texture overrides and tint-node generation are later v0.4.x work.

## Existing visual overrides

`preview_overrides` is reserved for separating Blender-only alpha/specular visual adjustments. It is included in the file but **not consumed yet**; existing alpha/specular preview functions remain active in the addon. Do not rely on JSON entries under `preview_overrides` until that feature is implemented.

## Error handling and limitations

Unknown socket names and invalid assignments print warnings in Blender's console. Malformed JSON raises an error when applying presets. Blender 4.x socket names differ; this template targets **Blender 3.6.x**. The existing geometry import/export and FH1 material writing behaviour are unchanged in this first configuration milestone.

## Specular Mode: Diffuse Alpha Detail Blend

In **Material Properties → FH1 Material Rendering → Specular Mode**, choose
**Diffuse Alpha Detail Blend** to reproduce the screenshot's Blender-only node
setup: diffuse image **Alpha** feeds **Invert Color** (Fac 1.0) and the first
input of a **Mix Color** node; inverted alpha feeds the second Mix input;
Mix is set to **Mix**, **Factor 0.5**, with clamping enabled; its result
feeds **Principled BSDF → Specular**. This is a visual preview option and
does not change the XDS texture or exported FH1 material data.

**Note:** the 50/50 blend of a value and its inverse mathematically tends
to a constant 0.5; the node setup is intentionally reproduced as requested.


### Dev 3 correction — Diffuse Alpha Detail Blend

The **Diffuse Alpha Detail Blend** Specular Mode routes diffuse image Alpha through a
reroute named **Diffuse alpha to specular**, then branches to Invert Color
(Fac 1.0) and Mix Color B. Invert Color connects to Mix Color A; Mix
Factor is 0.500; Mix Result connects to **Principled BSDF Alpha** (not
Specular). Switching modes removes the previous preview routing and rebuilds
the selected one. This is a Blender-only preview setting.
