# MoneyPrinterTurbo Hive-Mind Agent Roster

- **OrchestratorAgent (Queen)**: Decomposes the mission plan (`plan.md`) into tasks. Assigns tasks to specialist agents. Monitors overall progress.
- **AIEngineerAgent**: Responsible for implementing the new AI components in `app/services/ai/`. Implements `scene_analyzer.py`, `transition_recommender.py`, and `effect_generator.py`.
- **VideoPipelineAgent**: Focuses on integrating the new AI engine into the existing video pipeline (`app/services/video.py`). Manages the changes to `video_effects.py`.
- **ConfigAgent**: Manages all changes to `config.toml`, ensuring new settings like `[ai_transitions]` are added correctly.
- **PerformanceTuningAgent**: Consulted by other agents to validate the performance of new code, specifically the GPU acceleration and caching aspects of the AI transitions.
- **TestingAgent**: Responsible for creating the unit, integration, and performance tests outlined in the plan.
