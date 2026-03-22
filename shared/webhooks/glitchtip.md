# GlitchTip Webhook Instructions

You are receiving alert notifications from GlitchTip (Sentry-compatible error tracking).

GlitchTip sends a Slack-compatible payload with a "text" field and "attachments" array.
Each attachment represents an issue with title, link, culprit, color, and tag fields.

## Event Handling

### alert
- List each issue from the attachments array
- Note the project name and environment from the fields
- Include the title_link so the team can click through to investigate
- Flag if multiple issues are bundled in one alert (possible incident)
- Note the culprit (file/function where the error originated)

## Response Format

Lead with the number of issues and their severity (infer from color if present).
Keep it concise — include the issue title, project, environment, and link.
