## Feature: Add sorting options to the Villagers list

### Problem Description

In the **ACHNBrowserUI** app, the Villagers list view displays all villagers but provides no way to sort them. Users who want to find villagers by name or browse by species have to manually scan through the entire unsorted list.

### Acceptance Criteria

1. Users must be able to sort villagers by name and by species.
2. Sorting must use locale-aware string comparison.
3. A sort button in the navigation bar must present the available sort options.
4. The sort button icon must visually indicate whether a sort is currently active.
5. Setting the sort to the same value it already holds must reverse the sort order (ascending / descending). The initial sort direction must be ascending.
6. There must be a way to clear the active sort, restoring the default villager order.
7. When searching, search results must take priority over sorting.
8. French localization must be added for any new user-facing sort strings.
9. The existing search functionality and villager detail view must remain unchanged.

### Required API Surface

The implementation must expose these names (tests depend on them to compile):

- `VillagersViewModel.Sort` — `.name`, `.species`
- `VillagersViewModel.sort`, `VillagersViewModel.sortedVillagers`
