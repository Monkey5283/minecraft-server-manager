# MonkeyLobbyMusic

A dependency-free Paper 1.20.1 lobby radio containing Monkeycraft's two
original fifteen-minute NBS tracks. The station stays synchronized for all
players, alternates the tracks with a five-second pause, and plays entirely
through vanilla note-block sounds—no client mod or resource pack is required.

Player commands:

- `/radio now`
- `/radio toggle`
- `/radio volume <0-100>`

Operators can use `/radio skip`.

Build from the repository root with the existing MonkeyPortals wrapper:

```powershell
& minecraft-plugins\monkey-portals\gradlew.bat `
  -p minecraft-plugins\monkey-lobby-music clean test jar
```
