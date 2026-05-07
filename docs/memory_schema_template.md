# Marvin Memory (suki_memory.json) Schema Template

This template defines the structured memory of the Marvin Discord Bot. It is used to feed Marvin context about players and proactively initiate conversations.

## 1. Player Data Structure (players)
Each key under `players` is a Discord username.

```json
{
  "players": {
    "USERNAME": {
      "personal_info": {
        "food": null,        // Favorite food or dietary habits
        "clothing": null,    // Style of dress or notable accessories
        "housing": null,     // Location or living situation
        "transport": null,   // Commute method or vehicle preference
        "minecraft_id": null // Linked Minecraft account name (if any)
      },
      "likes": [],           // List of things the player enjoys
      "dislikes": [],        // List of things the player hates
      "taboos": [],          // Topics Marvin should strictly avoid with this player
      "suki_impression": "", // A paragraph describing Marvin's "inner monologue" about this person
      "highlight_of_the_day": "", // The most notable event from recent interactions
      "stats": {
        "interaction_count": 0,
        "pos_feedback": 0,   // Number of positive mood swings caused by this player
        "neg_feedback": 0,   // Number of negative/boring interactions
        "vul_feedback": 0    // Number of times the player expressed distress/vulnerability
      },
      "news_queue": [],      // Internal queue for personalized news hooks
      "bias_score": 0.0,     // Float (-10 to 10). Positive = Marvin cares more/is more reactive.
      "last_interacted_time": 0.0, // Unix timestamp of last interaction
      "relationship_stage": "陌生人", // "陌生人" | "熟人" | "老友" | "摯友"
      "relationship_note": "", // Context about the relationship evolution
      "emotional_highlights": [], // List of high-impact moments {moment, valence, timestamp}
      "behavioral_patterns": {} // Dictionary of player habits {key: value}
    }
  }
}
```

## 2. Proactive Topics (proactive_topics)
An array of "seed" conversations for Marvin to initiate during periods of silence.

```json
{
  "proactive_topics": [
    {
      "id": "unique_id",
      "title": "Topic Title",
      "target_players": ["player1", "player2"], // Who this topic is relevant to
      "script": "The base prompt for Marvin. Use @mentions.",
      "context_tags": ["tag1", "tag2"]
    }
  ]
}
```

## 3. Usage Guidelines for LLM
- **Language**: Preserve Traditional Chinese (zh-TW) for all descriptive fields (`suki_impression`, `script`, etc.).
- **Persona**: `suki_impression` should reflect Marvin's depressed, existential, and cynical perspective.
- **Dynamic Updates**: Fields in `personal_info` should stay concise (2-5 words), while `suki_impression` should be a descriptive paragraph.
