# Summary Prompt Templates

Reference templates for generating summaries with an external LLM (Claude, ChatGPT, etc.).
Copy the relevant prompt, paste it along with the transcript/document text, and upload
the result back via the review page or API.

---

## Short Summary Prompt

```
Given the following transcript/document, produce a 2-3 sentence executive summary.

Format Requirements:
- First sentence: What this is (meeting type, body, date) or (document type, topic)
- Second sentence: Key decisions, actions, or topics covered
- Third sentence (optional): Notable outcomes or next steps
- Output as plain text (no markdown)
- Max 300 characters

Tags Line:
After the summary, include a "Tags:" line with 3-5 keyword tags.
Format: Tags: budget, water-district, public-comment, zoning, appeal

Example Output:
The Shasta County Board of Supervisors held a regular meeting on Jan 14, 2026.
Key topics included the FY2026 budget adoption, a zoning variance appeal for
Anderson, and extended public comment on the proposed water rate increase.
Tags: budget, zoning, water-rates, anderson, public-comment
```

---

## Long Summary Prompt

```
Given the following transcript/document, produce a structured markdown summary.

Format Requirements:
Output as a markdown file with this structure:

### Metadata
- **Type**: [Meeting/Radio Show/Agenda Packet/Minutes]
- **Body/Source**: [Governing body or show name]
- **Date**: [YYYY-MM-DD]
- **Duration**: [if applicable]

### Executive Summary
[2-3 sentence overview -- same as short summary]

### Key Topics
1. **[Topic Name]** -- [1-2 sentence description of discussion/outcome]
2. **[Topic Name]** -- [...]

### Decisions & Actions
- [Decision or action item with responsible party if mentioned]

### Notable Quotes
> "[Direct quote]" -- [Speaker Name]
(Include 2-3 notable quotes if available)

### People Mentioned
- **[Name]** -- [Role/context of their involvement]

### Tags
budget, zoning, water-rates, public-comment
```

---

## Tips

- For meetings with transcripts, paste the full transcript text
- For agenda packets/minutes (PDF), paste the OCR text or manually extracted content
- For radio shows, paste the transcript with speaker labels
- The short summary is displayed as a quick preview; the long summary is the full reference
- Tags are used for cross-referencing and search enhancement
