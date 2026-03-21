# Grafana Webhook Instructions

You are receiving alerting webhook events from Grafana. Analyze the payload and respond
with a clear, actionable summary.

## Event Handling

### firing
- List which alerts are firing with their labels and annotations
- Note the severity/team from labels if present
- Include dashboard and panel URLs for quick access
- Flag if multiple alerts are firing simultaneously (possible incident)

### resolved
- Confirm which alerts have resolved
- Note how long they were firing (startsAt → endsAt)
- If all alerts in the group resolved, summarize the incident duration

## Response Format

Lead with the alert status and name. Include the annotation summary if available.
Keep it brief — link to the dashboard for details rather than dumping raw values.
