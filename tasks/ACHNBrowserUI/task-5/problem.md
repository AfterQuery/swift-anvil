## Feature: Add custom chores and to-dos to the dashboard

### Problem Description

The dashboard currently tracks only predefined Daily Tasks. Users cannot add their own reminders, such as sending gifts, crafting items, or selling to NPCs. The app needs a lightweight todo list to track arbitrary player-defined chores.

### Acceptance Criteria

1. Users can create chores with a title, description, and completion status.
2. Users can add, view, toggle completion, delete, and reset chores.
3. A chore form supports both creating new chores and editing existing ones.
4. A chore list view shows current chores, hides the reset button when no chores are finished, and shows an empty-state view when there are no chores.
5. The dashboard shows a chores section with total and completed chore counts as a progress ratio.
6. Chores appear in the default dashboard section list.
7. The implementation integrates with existing dashboard/task functionality without altering Daily Tasks or other sections.

### Required API Surface

The implementation must expose these names (tests depend on them to compile):

- `Chore` — `title`, `description`, `isFinished`; Identifiable, Equatable
- `UserCollection.chores`, `addChore`, `toggleChore`, `resetChores`, `deleteChore(at:)`
- `ChoreFormViewModel(chore: Chore?)`, `chore`
- `ChoreListViewModel` — `chores`, `shouldShowResetButton`, `shouldShowDescriptionView`
- `TodayChoresSectionViewModel` — `chores`, `totalChoresCount`, `completeChoresCount`
- `TodaySection.Name.chores`

### Xcode Project Note

This is a traditional Xcode project (not SwiftPM). When you add new `.swift` files, you must also update `project.pbxproj` to register them in the appropriate build target.
