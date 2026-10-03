# Changelog

Changes between consecutive builds of the EVE Online Static Data Export, generated from `deltas/*/manifest.json`.
It lists tables and counts only; the values are in `deltas/<build>/delta.jsonl.gz`.

## Build 3054210 (2025-10-10) - base 3049853

- 3 tables changed: +1 added, -0 removed, ~7 modified, 0 schema operations
  - `skinMaterials`: +1 -0 ~0
  - `skins`: +0 -0 ~6
  - `types`: +0 -0 ~1

## Build 3049853 (2025-10-07) - base 3049203

- 2 tables changed: +0 added, -0 removed, ~2 modified, 0 schema operations
  - `typeDogma`: +0 -0 ~1
  - `types`: +0 -0 ~1

## Build 3049203 (2025-10-06) - base 3044286

- 10 tables changed: +362 added, -0 removed, ~57 modified, 0 schema operations
  - `dbuffCollections`: +1 -0 ~0
  - `graphics`: +5 -0 ~1
  - `icons`: +13 -0 ~0
  - `npcCorporations`: +0 -0 ~4
  - `skinLicenses`: +39 -0 ~0
  - `skinMaterials`: +6 -0 ~1
  - `skins`: +39 -0 ~7
  - `typeDogma`: +83 -0 ~12
  - `typeMaterials`: +3 -0 ~0
  - `types`: +173 -0 ~32

## Build 3044286 (2025-10-01) - base 3041737

- 1 tables changed: +0 added, -0 removed, ~1 modified, 0 schema operations
  - `types`: +0 -0 ~1

## Build 3041737 (2025-09-29) - base 3031812

- 12 tables changed: +424 added, -426 removed, ~24813 modified, 62 schema operations
  - `dogmaAttributes`: +0 -0 ~2775 [schema: drop_path x1, rename_path x1]
  - `dogmaEffects`: +0 -0 ~3286 [schema: drop_path x2, rename_path x2]
  - `dynamicItemAttributes`: +0 -0 ~0
  - `mapAsteroidBelts`: +0 -0 ~46 [schema: add_path x3, drop_path x3, rename_path x5]
  - `mapMoons`: +0 -0 ~137 [schema: rename_path x8]
  - `mapPlanets`: +0 -0 ~43 [schema: add_path x2, drop_path x2, rename_path x6]
  - `npcCharacters`: +424 -4 ~10878 [renamed from agents] [schema: add_path x20, rename_path x4, rename_table x1]
  - `npcStations`: +0 -0 ~170
  - `researchAgents`: +0 -422 ~0
  - `skinMaterials`: +0 -0 ~817 [schema: drop_path x1]
  - `skins`: +0 -0 ~6660 [schema: drop_path x1]
  - `typeDogma`: +0 -0 ~1

## Build 3031812 (2025-09-22) - base 3030547

- 4 tables changed: +0 added, -0 removed, ~417528 modified, 34 schema operations
  - `dynamicItemAttributes`: +0 -0 ~12 [schema: add_path x1]
  - `mapAsteroidBelts`: +0 -0 ~7385 [schema: add_path x8]
  - `mapMoons`: +0 -0 ~342170 [schema: add_path x8, rename_path x3]
  - `mapPlanets`: +0 -0 ~67961 [schema: add_path x10, drop_path x1, rename_path x3]
