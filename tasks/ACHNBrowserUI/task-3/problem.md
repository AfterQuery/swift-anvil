## Feature: Track per-variant collection progress for items

### Problem Description

In the **ACHNBrowserUI** app, the favorite (star) system for items is binary: an item is either liked or not. However, many catalog items have multiple color or style variants. Users have no way to tell from the UI whether they have collected some but not all variants of a particular item, and tapping the favorite button on a multi-variant item simply toggles the parent item rather than letting users select specific variants.

This makes it difficult to track per-variant collection progress.

### Acceptance Criteria

1. The like system must support three visual states for multi-variant items: no variants collected, some variants collected (partial), and all variants collected (complete).
2. Each state must use a distinct icon so users can see collection progress at a glance.
3. Tapping the favorite button on a multi-variant item must present a variant selection UI rather than directly toggling the parent item.
4. The variant selection UI must be accessible from both the item list and the item detail view.
5. Liking the first variant of an item must automatically add the parent item to the collection. Unliking the last variant must automatically remove it.
6. The icon state must update immediately when variants are toggled.
7. Items with zero or one variant must retain the existing binary like behavior and not show partial states.

### Required API Surface

The implementation must expose these names (tests depend on them to compile):

- `Item.hasSomeVariations`
- `VariantsCompletionStatus` — `.unstarted`, `.partial`, `.complete`
- `[String: [Variant]].completionStatus(for:)`
- `UserCollection.toggleVariant(item:variant:) -> Bool`
