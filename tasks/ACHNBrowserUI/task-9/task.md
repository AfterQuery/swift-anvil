## Feature: Track today's villager visits on the Dashboard

### Problem Description

In **ACHNBrowserUI**, the Today Dashboard can track custom chores and daily tasks, but it has no way to track player interactions with island residents. In Animal Crossing, players can gift each of their 10 villagers once per day. There is currently no in-app way to record which residents have been visited or gifted today.

Villagers that the user has marked as residents (via the Home button on the villager detail view) should automatically appear in a new "Villager Visits" section of the Today Dashboard. The user can then check off each resident after visiting or gifting them, and reset the section at the start of a new day — exactly mirroring how the Custom Chores section works.

### Acceptance Criteria

1. A new `TodaySection.Name.villagerVisits` section type must be added and included in the default section list.
2. `UserCollection` must track which resident villagers have been visited today via a `visitedResidents` property, support toggling a villager's visited state, and support resetting all visited states. Note: the section is named "Villager Visits" but the `UserCollection` API uses "resident" terminology — do not name the properties `villagerVisits`, `toggleVillagerVisit`, or `resetVillagerVisits`.
3. A long-press on a villager in the visits section must open the villager detail view so the player can check gift preferences.
4. The section must hide the reset button when no villagers have been visited yet.
5. German localization strings must be provided for the new section title.
6. Existing Today Dashboard sections (daily tasks, chores, etc.) must remain unchanged.

### Required API Surface

- `TodaySection.Name.villagerVisits`
- `UserCollection.visitedResidents: [Villager]`
- `UserCollection.toggleVisitedResident(villager:)`
- `UserCollection.resetVisitedResidents()`
