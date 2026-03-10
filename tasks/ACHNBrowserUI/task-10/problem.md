## Feature: Add creator and custom design tracking to the Collection tab

### Problem Description

The **ACHNBrowserUI** app has no way for users to save and organize Animal Crossing custom design codes (creator codes like `MA-XXXX-XXXX-XXXX` and item codes like `MO-XXXX-XXXX-XXXX`). Players frequently share these codes online and need a place to store them within the app.

Additionally, the Collection tab's segmented picker currently has four top-level segments (Items, Villagers, Critters, Lists). Adding more segments causes label truncation on smaller screens. The "Critters" section is also redundant since critter data is already featured on the Dashboard. The navigation structure needs to be reorganized to accommodate new collection categories without overcrowding the picker.

### Acceptance Criteria

1. Users can create, view, and delete custom design entries with name, code, and description.
2. Design codes are validated against the MA/MO prefix format and formatted consistently.
3. Saved designs appear in a dedicated list within the Collection tab.
4. The Collection picker no longer truncates on small screens.
5. Critters and designs are accessible via a secondary navigation point within Collection.
6. Empty collection states display a helpful message to the user.
7. All design data persists across app launches.
8. Existing collection features (items, villagers, lists) remain functional.
9. The app builds and runs without regressions.

### Required API Surface

The implementation must expose these names (tests depend on them to compile):

- `Design` — Backend; `title`, `code`, `description`, `hasValidCode`; Identifiable, Equatable
- `UserCollection.designs`
- `DesignFormViewModel(design: Design?)`, `design`
- `DesignRowViewModel(design: Design)`, `category: String` (returns `"Creator"` for codes with `MA` prefix, `"Item"` for `MO` prefix), `code: String` (formats the raw stored code as `"XX-XXXX-XXXX-XXXX"` by inserting dashes after the 2-char prefix then every 4 characters, e.g. `"MA667931515180"` → `"MA-6679-3151-5180"`)
- `CollectionMoreDetailViewModel`, `rows`, `Row.critters`, `Row.designs`
- `Tabs.more`
- `MessageView(string:)`, `MessageView(collectionName:)`, `MessageView(noResultsFor:)`

### Xcode Project Note

This is a traditional Xcode project (not SwiftPM). New `.swift` files must be registered in `project.pbxproj` or they will not compile.
