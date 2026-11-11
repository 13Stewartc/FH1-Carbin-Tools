# FH1-Carbin Tools

**FH1-Carbin Tools** is a Blender 3.6 add-on designed to import, modify and export vehicle models from **Forza Horizon (2012)**.

The project aims to make FH1 vehicle modding more accessible by allowing users to work directly with the game's proprietary `.carbin` model format inside Blender.

## Features

- Import FH1 vehicle geometry into Blender.
- Export modified geometry back into `.carbin` files.
- Support for exterior LOD1–LOD5 and cockpit LOD0 models.
- Add custom geometry to existing vehicle components.
- Preserve original vehicle hierarchy, UV mapping and normals.
- Inspect and work with existing vehicle materials.
- Experimental support for damage-related vertex data and deformation.

## Current limitations

- Creating additional material sections is experimental. New sections may be recognised by model viewers but fail to render in-game.
- Custom texture and material-instance exporting remains under development.
- Damage deformation support is experimental and may behave differently between exterior and cockpit models.
- Compatibility may vary between vehicles and CARBIN variants.

## Compatibility

- **Blender:** 3.6
- **Game:** Forza Horizon (2012)
- **Mod testing:** Forza Horizon Re-Imagined

## Project status

FH1-Carbin Tools is an ongoing reverse-engineering and modding project. Some functionality remains experimental, and exported models should be tested before use.

This is an unofficial community project and is not affiliated with Microsoft, Xbox Game Studios, Playground Games or Turn 10 Studios.

## Non-Commercial Use

FH1-Carbin Tools is a free, community-driven project created for Forza Horizon 1 modding, research and preservation.

**No profiteering is permitted.**

The software and its derivatives must not be sold, monetised, redistributed for payment, included in paid mod packs, or placed behind paywalls without explicit written permission from the original author.

Users may share, discuss and contribute to the project, provided the original author receives appropriate credit and the project's non-commercial terms are respected.

FH1-Carbin Tools is an unofficial project and is not affiliated with Microsoft, Playground Games, Turn 10 Studios or Xbox Game Studios.

## Credits & Acknowledgements

Developed by **13Stewartc** with reference to community reverse-engineering research and existing Forza modding tools.

Special thanks to the developers and contributors behind Forza Studio, Forza Horizon Re-Imagined and the wider Forza modding community.

See [CREDITS.md](CREDITS.md) for acknowledgements, research references and third-party attributions.

## Development Assistance

### Vibe Coded with ChatGPT+ 🤖💜

FH1-Carbin Tools was developed by **13Stewartc**, with extensive AI-assisted development provided by **OpenAI's ChatGPT Plus**.

ChatGPT assisted with Python scripting, reverse engineering the proprietary CARBIN format, binary analysis, debugging, and implementing the Blender import/export functionality.

All project direction, feature requirements, hands-on testing, in-game validation, and final development decisions were handled by **13Stewartc**.

Development was powered by questionable amounts of caffeine, relentless trial and error, countless hours of debugging, and **an unreasonable number of exploded Volkswagen Corrados**.

While ChatGPT contributed extensively to the development process, it also occasionally demonstrated a remarkable ability to turn perfectly functional vehicles into abstract art.

*Built with curiosity, persistence, a healthy disregard for sleep, and the shared determination to make a 2012 racing game do things it was never supposed to do.*
