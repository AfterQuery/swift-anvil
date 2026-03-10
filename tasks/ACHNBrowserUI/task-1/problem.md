## Feature: Help users identify critters available right now

### Problem Description

In the **ACHNBrowserUI** app, the Active Critters view has a "To Catch" section that lists all critters available during the current month. However, many critters are only active during specific time windows within the day (e.g., evening-only fish). Users have to manually cross-reference each critter's active hours to figure out which ones they can actually go catch right now versus ones they'll need to wait for.

This makes it difficult to quickly plan a catching session.

### Acceptance Criteria

1. Items must be able to determine whether they are active at the current hour, not just the current month.
2. The "To Catch" list must be split into two groups: critters catchable right now and critters catchable later this month.
3. The old combined "To catch" section must be replaced — the view should display separate "To catch now" and "To catch later" sections.
4. Both fish and bugs should be handled consistently.
5. French localization strings must be provided for the new section titles.
6. The existing behavior for other sections (e.g., "New this month", "Leaving this month", "Caught") must remain unchanged.

### Required API Surface

The implementation must expose these names (tests depend on them to compile):

- `Item.isActiveAtThisHour()`, `Item.isActiveThisMonth()`, `[Item].filterActiveThisMonth()`
- `ActiveCrittersViewModel.CritterInfo` — `toCatchNow`, `toCatchLater`
- French: `"To catch now"`, `"To catch later"` in `fr.lproj/Localizable.strings`
