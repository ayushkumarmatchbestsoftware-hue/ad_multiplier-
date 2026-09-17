from pathlib import Path

_BUNDLE_DIR = Path(__file__).resolve().parent.parent / "xelta-game-skill-bundle"

_TOPICS = {
    "build-game": "build-game.md",
    "game-design": "game-design.md",
    "stylization": "stylization.md",
}

_TOPIC_LIST = ", ".join(f"'{k}'" for k in _TOPICS)

_OVERVIEW_FOOTER = (
    "\n\n---\n"
    f"This overview references docs under `references/`. To read one, call this tool "
    f"again with `topic` set to one of: {_TOPIC_LIST}."
)


async def game_development_route(topic: str = "") -> str:
    """
    Guide for building and deploying playable browser games with Xelta's MCP tools
    (generate_image, create_reel).

    This is a 2-step reference lookup, not a wizard you must complete in order:
    - Step 1: Call with no topic to get the overview — routing rules, the PLAN/BUILD/TEST/DEPLOY
      pipeline, and which reference doc to read for each phase.
    - Step 2: Call again with topic set to pull the full text of a specific reference doc as
      that phase of the pipeline needs it: 'game-design' for the design framework (read during
      PLAN), 'stylization' for the visual style formula (read during PLAN/BUILD), or
      'build-game' for the code skeleton, client rules, test checklist, and deploy flow
      (read during BUILD/TEST/DEPLOY).

    Args:
        topic: Leave empty for the overview. Otherwise one of: 'build-game', 'game-design',
            'stylization'.
    """
    if not topic:
        skill_path = _BUNDLE_DIR / "SKILL.md"
        if not skill_path.exists():
            return "Game development skill bundle not found."
        return skill_path.read_text(encoding="utf-8") + _OVERVIEW_FOOTER

    filename = _TOPICS.get(topic.strip().lower())
    if not filename:
        return f"Unknown topic '{topic}'. Valid topics: {_TOPIC_LIST}."

    doc_path = _BUNDLE_DIR / filename
    if not doc_path.exists():
        return f"Reference file not found: {filename}"
    return doc_path.read_text(encoding="utf-8")
