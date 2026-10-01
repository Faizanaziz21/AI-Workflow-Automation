"""Enterprise workflow template library."""

from __future__ import annotations

from app.templates.builder import TemplateSpec, conn

AI = {"connection_id": conn("ai")}


def customer_support() -> TemplateSpec:
    """Flagship: Enterprise Customer Support Automation."""
    t = TemplateSpec(
        slug="customer-support-automation",
        name="Enterprise Customer Support Automation",
        category="Support",
        summary="AI triage, account context, knowledge-grounded replies, confidence-gated auto-send, human review, "
        "enterprise escalation and full post-resolution analytics.",
        description=(
            "Customer email arrives → identity extraction → AI classification + sentiment → CRM account and ticket "
            "history lookup → knowledge-base search → AI drafts a grounded reply. High-confidence, low-risk replies are "
            "sent automatically; everything else goes to a support agent for review/editing. Enterprise customers with "
            "very negative sentiment are escalated to the senior support manager with a P1 ticket and Slack alert. "
            "After resolution the ticket is summarised, the CRM is updated, the root cause classified, an analytics "
            "record written and a satisfaction survey sent."
        ),
        tags=["support", "ai", "crm", "human-in-the-loop", "flagship"],
        connection_roles={
            "ai": "category:ai",
            "crm": "capability:crm.upsert_contact",
            "helpdesk": "http_rest",
            "email": "capability:email.send",
            "slack": "slack",
            "tickets": "capability:ticket.create",
            "analytics_db": "postgres",
        },
        featured=True,
        variables={
            "auto_send_confidence": 0.9,
            "support_from": "support@acme.example",
            "escalation_channel": "#support-escalations",
        },
        settings={"max_parallel_nodes": 16, "mask_fields": ["phone"]},
    )
    n = t.node
    n(
        "email_in",
        "trigger.webhook",
        "Customer email received",
        {
            "authentication": "signature",
            "idempotency_header": "Message-Id",
            "correlation_key": "{{ body['from'] }}",
            "sample_payload": {
                "from": "jane@globex.com",
                "subject": "Charged twice",
                "text": "Hi, I was charged twice...",
            },
        },
    )
    n(
        "identity",
        "ai.extract",
        "Extract customer identity",
        {
            "text": "From: {{ trigger.body['from'] }}\nSubject: {{ trigger.body.subject }}\n\n{{ trigger.body.text }}",
            "instructions": "Identify the customer and any references in this support email.",
            "fields": [
                {"name": "email", "type": "string", "description": "Customer email address"},
                {"name": "name", "type": "string"},
                {"name": "company", "type": "string"},
                {"name": "order_id", "type": "string", "description": "Order or invoice reference if mentioned"},
            ],
            "model": AI,
        },
    )
    n(
        "classify",
        "ai.email_router",
        "Classify request",
        {
            "sender": "{{ trigger.body['from'] }}",
            "subject": "{{ trigger.body.subject }}",
            "body": "{{ trigger.body.text }}",
            "route": False,
            "model": AI,
        },
    )
    n("sentiment", "ai.sentiment", "Detect sentiment", {"text": "{{ trigger.body.text }}", "model": AI})
    n(
        "account",
        "crm.find_contact",
        "Look up customer account",
        {
            "connection_id": conn("crm"),
            "email": "{{ default(nodes.identity.output.data.email, trigger.body['from']) }}",
        },
        retry={"max_attempts": 3, "initial_interval_seconds": 2},
    )
    n(
        "history",
        "data.http_request",
        "Retrieve previous tickets",
        {
            "connection_id": conn("helpdesk"),
            "url": "/tickets",
            "query": {"email": "{{ default(nodes.identity.output.data.email, trigger.body['from']) }}", "limit": 5},
        },
        retry={"max_attempts": 3, "initial_interval_seconds": 2},
        on_error="continue",
    )
    n(
        "kb",
        "data.http_request",
        "Search knowledge base",
        {
            "connection_id": conn("helpdesk"),
            "url": "/kb/search",
            "query": {"q": "{{ trigger.body.subject }}", "limit": 3},
        },
        retry={"max_attempts": 3, "initial_interval_seconds": 2},
        on_error="continue",
    )
    n("context", "logic.merge", "Gather context", {"mode": "object"})
    n(
        "priority",
        "logic.set_variable",
        "Determine priority",
        {
            "assignments": {
                "tier": "{{ default(nodes.account.output.contact.properties.tier, 'standard') if nodes.account.output.found else 'unknown' }}",
                "category": "{{ nodes.classify.output.category }}",
                "sentiment": "{{ nodes.sentiment.output.sentiment }}",
                "priority": "{{ 'P1' if nodes.sentiment.output.sentiment == 'very_negative' or nodes.classify.output.urgency == 'critical' "
                "else ('P2' if nodes.classify.output.urgency == 'high' or nodes.classify.output.category == 'complaint' else 'P3') }}",
                "customer_email": "{{ default(nodes.identity.output.data.email, trigger.body['from']) }}",
            }
        },
    )
    n(
        "draft",
        "ai.decision",
        "AI drafts response",
        {
            "goal": "Draft the best reply to the customer and assess whether it can be sent without human review.",
            "policy": (
                "Only use facts from the knowledge-base articles and account data. Never promise refunds above $500, "
                "legal outcomes or timelines not in the articles. Risk is 'high' for refunds, legal, security or "
                "angry enterprise customers; 'medium' for account changes; otherwise 'low'."
            ),
            "context": {
                "email": "{{ trigger.body }}",
                "classification": "{{ nodes.classify.output }}",
                "sentiment": "{{ nodes.sentiment.output }}",
                "account": "{{ nodes.account.output.contact }}",
                "previous_tickets": "{{ nodes.history.output.body }}",
                "articles": "{{ nodes.kb.output.body }}",
                "priority": "{{ vars.priority }}",
            },
            "options": ["reply", "needs_agent"],
            "output_schema": {
                "reply_subject": {"type": "string"},
                "reply_body": {"type": "string"},
                "risk": {"type": "string", "enum": ["low", "medium", "high"]},
                "issue_type": {"type": "string"},
            },
            "model": AI,
        },
    )
    # Escalation path
    n(
        "escalate_check",
        "logic.if",
        "Enterprise + very negative?",
        {"condition": "{{ vars.tier == 'enterprise' and vars.sentiment == 'very_negative' }}"},
    )
    n(
        "p1_ticket",
        "ticket.create",
        "Escalate to senior support manager",
        {
            "connection_id": conn("tickets"),
            "project": "SUP",
            "issue_type": "Task",
            "priority": "Highest",
            "summary": "[ESCALATION] {{ trigger.body.subject }} — {{ vars.customer_email }}",
            "description": "Enterprise customer with very negative sentiment.\n\nCategory: {{ vars.category }}\n"
            "Sentiment: {{ nodes.sentiment.output.reasoning_summary }}\n\n{{ trigger.body.text }}",
            "labels": ["escalation", "enterprise"],
        },
    )
    n(
        "slack_escalation",
        "slack.post_message",
        "Notify escalation channel",
        {
            "connection_id": conn("slack"),
            "channel": "{{ vars.escalation_channel }}",
            "text": ":rotating_light: *{{ vars.priority }} escalation* for {{ vars.customer_email }} ({{ vars.tier }}) — "
            "{{ trigger.body.subject }}. Ticket {{ nodes.p1_ticket.output.ticket.key }}",
        },
    )
    # Response path
    n(
        "auto_check",
        "logic.if",
        "Confident & low risk?",
        {
            "condition": "{{ nodes.draft.output.decision == 'reply' and nodes.draft.output.confidence >= vars.auto_send_confidence "
            "and nodes.draft.output.risk == 'low' }}"
        },
    )
    n(
        "send_auto",
        "comm.email",
        "Send response automatically",
        {
            "connection_id": conn("email"),
            "to": ["{{ vars.customer_email }}"],
            "subject": "{{ nodes.draft.output.reply_subject }}",
            "text": "{{ nodes.draft.output.reply_body }}",
        },
    )
    n(
        "agent_review",
        "human.manual_review",
        "Assign to support agent",
        {
            "title": "Review reply to {{ vars.customer_email }} ({{ vars.priority }})",
            "description": "AI confidence {{ round(nodes.draft.output.confidence * 100) }}%, risk {{ nodes.draft.output.risk }}. "
            "Edit the reply if needed, then approve to send.",
            "context": {
                "email": "{{ trigger.body }}",
                "ai_reasoning": "{{ nodes.draft.output.reasoning_summary }}",
                "account": "{{ nodes.account.output.contact }}",
            },
            "editable_fields": {
                "subject": "{{ nodes.draft.output.reply_subject }}",
                "body": "{{ nodes.draft.output.reply_body }}",
            },
            "approvers": {"role": "operator"},
            "due_in_hours": 8,
            "escalation": {"after_hours": 4, "escalate_to": {"role": "org_admin"}, "max_escalations": 1},
            "on_timeout": "timeout_branch",
        },
    )
    n(
        "send_reviewed",
        "comm.email",
        "Send reviewed response",
        {
            "connection_id": conn("email"),
            "to": ["{{ vars.customer_email }}"],
            "subject": "{{ default(nodes.agent_review.output.data.subject, nodes.draft.output.reply_subject) }}",
            "text": "{{ default(nodes.agent_review.output.data.body, nodes.draft.output.reply_body) }}",
        },
    )
    n("resolved", "logic.merge", "Resolution", {"mode": "list"})
    # Post-resolution
    n(
        "summary",
        "ai.summarize",
        "Summarize ticket",
        {
            "text": "Customer: {{ trigger.body.text }}\n\nReply: {{ default(nodes.agent_review.output.data.body, nodes.draft.output.reply_body) }}",
            "style": "ticket_resolution",
            "max_words": 80,
            "model": AI,
        },
    )
    n(
        "crm_note",
        "crm.add_note",
        "Update CRM",
        {
            "connection_id": conn("crm"),
            "contact_id": "{{ default(nodes.account.output.contact.id, '') }}",
            "body": "Support ticket ({{ vars.priority }}, {{ vars.category }}): {{ nodes.summary.output.summary }}",
        },
        on_error="continue",
    )
    n(
        "root_cause",
        "ai.classify",
        "Classify root cause",
        {
            "text": "{{ nodes.summary.output.summary }}",
            "categories": [
                {"name": "product_bug", "description": "Defect in the product"},
                {"name": "billing_error", "description": "Incorrect charge or invoice"},
                {"name": "user_error", "description": "Misunderstanding or misuse"},
                {"name": "feature_gap", "description": "Missing capability"},
                {"name": "service_outage", "description": "Downtime or degradation"},
                {"name": "other"},
            ],
            "model": AI,
        },
    )
    n(
        "analytics",
        "postgres.query",
        "Write analytics record",
        {
            "connection_id": conn("analytics_db"),
            "sql": (
                "INSERT INTO support_analytics (execution_id, customer_email, category, sentiment, priority, tier, "
                "auto_sent, root_cause, ai_confidence, created_at) VALUES (:execution_id, :email, :category, :sentiment, "
                ":priority, :tier, :auto_sent, :root_cause, :confidence, now())"
            ),
            "params": {
                "execution_id": "{{ execution.id }}",
                "email": "{{ vars.customer_email }}",
                "category": "{{ vars.category }}",
                "sentiment": "{{ vars.sentiment }}",
                "priority": "{{ vars.priority }}",
                "tier": "{{ vars.tier }}",
                "auto_sent": "{{ nodes.send_auto.status == 'COMPLETED' }}",
                "root_cause": "{{ nodes.root_cause.output.category }}",
                "confidence": "{{ nodes.draft.output.confidence }}",
            },
        },
        retry={"max_attempts": 5, "initial_interval_seconds": 5},
    )
    n(
        "survey",
        "comm.email",
        "Send satisfaction survey",
        {
            "connection_id": conn("email"),
            "to": ["{{ vars.customer_email }}"],
            "subject": "How did we do? (ref {{ execution.id[:8] }})",
            "text": "Thanks for contacting support. Rate your experience: https://survey.acme.example/csat?ref={{ execution.id }}",
        },
    )
    t.chain("email_in", "identity")
    for nid in ("classify", "sentiment", "account", "history", "kb"):
        t.edge("identity", nid)
        t.edge(nid, "context")
    t.chain("context", "priority", "draft")
    t.edge("draft", "escalate_check")
    t.edge("escalate_check", "p1_ticket", "true")
    t.edge("p1_ticket", "slack_escalation")
    t.edge("draft", "auto_check")
    t.edge("auto_check", "send_auto", "true")
    t.edge("auto_check", "agent_review", "false")
    t.edge("agent_review", "send_reviewed", "approved")
    t.edge("send_auto", "resolved")
    t.edge("send_reviewed", "resolved")
    t.chain("resolved", "summary", "crm_note", "root_cause", "analytics", "survey")
    return t


def sales_lead() -> TemplateSpec:
    t = TemplateSpec(
        slug="ai-sales-lead-qualification",
        name="AI Sales Lead Qualification",
        category="Sales",
        summary="Validate, enrich, AI-classify and score inbound leads, sync to CRM, send personalised outreach with "
        "approval for large deals, notify sales, and escalate if the lead goes quiet for 3 days.",
        description="End-to-end inbound lead pipeline with a durable 3-day response wait and analytics write-back.",
        tags=["sales", "ai", "crm", "approval"],
        connection_roles={
            "ai": "category:ai",
            "enrichment": "http_rest",
            "crm": "capability:crm.upsert_contact",
            "email": "capability:email.send",
            "slack": "slack",
            "analytics_db": "postgres",
        },
        variables={"approval_threshold": 50000},
    )
    n = t.node
    n(
        "lead_in",
        "trigger.webhook",
        "Incoming sales lead",
        {"authentication": "signature", "correlation_key": "{{ body.email }}"},
    )
    n(
        "validate",
        "logic.if",
        "Validate lead",
        {
            "conditions": [
                {"left": "{{ trigger.body.email }}", "operator": "matches", "right": "^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$"},
                {"left": "{{ trigger.body.company }}", "operator": "is_not_empty"},
            ]
        },
    )
    n("reject", "logic.stop", "Invalid lead", {"outcome": "success", "message": "Lead failed validation"})
    n(
        "enrich",
        "data.http_request",
        "Enrich company",
        {
            "connection_id": conn("enrichment"),
            "url": "/companies",
            "query": {"domain": "{{ split(trigger.body.email, '@')[1] }}"},
        },
        on_error="continue",
    )
    n(
        "classify",
        "ai.lead_classifier",
        "Classify lead with AI",
        {"lead": {"form": "{{ trigger.body }}", "company": "{{ nodes.enrich.output.body }}"}, "model": AI},
    )
    n(
        "score",
        "logic.set_variable",
        "Calculate lead score",
        {
            "assignments": {
                "score": "{{ ({'high': 60, 'medium': 35, 'low': 10}[nodes.classify.output.priority]) + "
                "({'enterprise': 40, 'mid_market': 25, 'smb': 10, 'startup': 15, 'not_a_fit': -50}[nodes.classify.output.classification]) }}",
                "deal_value": "{{ default(nodes.classify.output.estimated_deal_value, 0) }}",
            }
        },
    )
    n(
        "dupe",
        "crm.find_contact",
        "Search CRM for duplicate",
        {"connection_id": conn("crm"), "email": "{{ trigger.body.email }}"},
    )
    n(
        "upsert",
        "crm.upsert_contact",
        "Create/update CRM contact",
        {
            "connection_id": conn("crm"),
            "email": "{{ trigger.body.email }}",
            "first_name": "{{ trigger.body.first_name }}",
            "last_name": "{{ trigger.body.last_name }}",
            "company": "{{ trigger.body.company }}",
            "lifecycle_stage": "lead",
            "properties": {"lead_score": "{{ vars.score }}"},
        },
    )
    n(
        "outreach",
        "ai.prompt",
        "Generate personalised outreach",
        {
            "system": "You write concise, specific B2B outreach emails. No hype, no invented facts.",
            "prompt": "Write a first-touch email to {{ trigger.body.first_name }} at {{ trigger.body.company }}. "
            "Their message: {{ trigger.body.message }}. Segment: {{ nodes.classify.output.classification }}.",
            "response_format": "json",
            "output_schema": {
                "type": "object",
                "properties": {"subject": {"type": "string"}, "body": {"type": "string"}},
                "required": ["subject", "body"],
                "additionalProperties": False,
            },
            "model": AI,
        },
    )
    n(
        "big_deal",
        "logic.if",
        "Deal value above threshold?",
        {"condition": "{{ vars.deal_value > vars.approval_threshold }}"},
    )
    n(
        "approve",
        "human.approval",
        "Sales manager approval",
        {
            "title": "Approve outreach to {{ trigger.body.company }} (est. ${{ vars.deal_value }})",
            "context": {"lead": "{{ trigger.body }}", "email": "{{ nodes.outreach.output.data }}"},
            "approvers": {"role": "approver"},
            "due_in_hours": 24,
            "on_timeout": "approve",
        },
    )
    n(
        "send",
        "comm.email",
        "Send email",
        {
            "connection_id": conn("email"),
            "to": ["{{ trigger.body.email }}"],
            "subject": "{{ nodes.outreach.output.data.subject }}",
            "text": "{{ nodes.outreach.output.data.body }}",
        },
    )
    n(
        "notify",
        "slack.post_message",
        "Notify salesperson",
        {
            "connection_id": conn("slack"),
            "channel": "#sales-leads",
            "text": "New {{ nodes.classify.output.priority }}-priority lead: {{ trigger.body.company }} (score {{ vars.score }})",
        },
    )
    n(
        "task",
        "crm.create_task",
        "Create follow-up task",
        {
            "connection_id": conn("crm"),
            "contact_id": "{{ nodes.upsert.output.contact.id }}",
            "subject": "Follow up with {{ trigger.body.first_name }} ({{ trigger.body.company }})",
            "due_at": "{{ date_add(now(), days=3) }}",
            "priority": "HIGH",
        },
    )
    n(
        "wait_reply",
        "logic.wait_until",
        "Track response (3 days)",
        {
            "mode": "event",
            "event_key": "lead-reply:{{ lower(trigger.body.email) }}",
            "timeout": 3,
            "timeout_unit": "days",
        },
    )
    n(
        "replied",
        "crm.add_note",
        "Log reply",
        {
            "connection_id": conn("crm"),
            "contact_id": "{{ nodes.upsert.output.contact.id }}",
            "body": "Lead replied: {{ nodes.wait_reply.output.payload }}",
        },
    )
    n(
        "escalate",
        "slack.post_message",
        "Escalate: no response",
        {
            "connection_id": conn("slack"),
            "channel": "#sales-managers",
            "text": ":warning: No reply from {{ trigger.body.company }} after 3 days — please follow up.",
        },
    )
    n(
        "analytics",
        "postgres.query",
        "Write to analytics",
        {
            "connection_id": conn("analytics_db"),
            "sql": "INSERT INTO lead_analytics (email, company, score, segment, replied, created_at) "
            "VALUES (:email, :company, :score, :segment, :replied, now())",
            "params": {
                "email": "{{ trigger.body.email }}",
                "company": "{{ trigger.body.company }}",
                "score": "{{ vars.score }}",
                "segment": "{{ nodes.classify.output.classification }}",
                "replied": "{{ nodes.wait_reply.output.received }}",
            },
        },
    )
    n("done", "logic.merge", "Outcome", {"mode": "list"})
    t.chain("lead_in", "validate")
    t.edge("validate", "reject", "false")
    t.edge("validate", "enrich", "true")
    t.chain("enrich", "classify", "score", "dupe", "upsert", "outreach", "big_deal")
    t.edge("big_deal", "approve", "true")
    t.edge("approve", "send", "approved")
    t.edge("big_deal", "send", "false")
    t.edge("send", "notify")
    t.edge("send", "task")
    t.edge("task", "wait_reply")
    t.edge("wait_reply", "replied", "received")
    t.edge("wait_reply", "escalate", "timeout")
    t.edge("replied", "done")
    t.edge("escalate", "done")
    t.edge("done", "analytics")
    return t


def onboarding() -> TemplateSpec:
    t = TemplateSpec(
        slug="employee-onboarding",
        name="Employee Onboarding",
        category="HR",
        summary="Provision accounts, equipment ticket, Drive folder and welcome messages in parallel; collect manager "
        "input; gate privileged access behind IT approval; remind on the start date.",
        description="Triggered by an HRIS 'employee.hired' event.",
        tags=["hr", "it", "parallel", "approval"],
        connection_roles={
            "it_api": "http_rest",
            "tickets": "capability:ticket.create",
            "drive": "google_drive",
            "slack": "slack",
            "email": "capability:email.send",
        },
    )
    n = t.node
    n("hired", "trigger.api_event", "Employee hired", {"event_name": "employee.hired"})
    n("fan", "logic.parallel", "Provision in parallel", {"branches": 4})
    n(
        "accounts",
        "data.http_request",
        "Create IT accounts",
        {
            "connection_id": conn("it_api"),
            "method": "POST",
            "url": "/accounts",
            "body": {
                "email": "{{ trigger.data.email }}",
                "name": "{{ trigger.data.name }}",
                "department": "{{ trigger.data.department }}",
            },
        },
        retry={"max_attempts": 4, "initial_interval_seconds": 5},
    )
    n(
        "laptop",
        "ticket.create",
        "Equipment ticket",
        {
            "connection_id": conn("tickets"),
            "project": "IT",
            "summary": "Laptop for {{ trigger.data.name }}",
            "description": "Start date {{ trigger.data.start_date }}, role {{ trigger.data.title }}",
            "labels": ["onboarding"],
        },
    )
    n(
        "folder",
        "google_drive.create_folder",
        "Create Drive folder",
        {"connection_id": conn("drive"), "name": "Onboarding — {{ trigger.data.name }}"},
    )
    n(
        "welcome_slack",
        "slack.direct_message",
        "Welcome DM",
        {
            "connection_id": conn("slack"),
            "email": "{{ trigger.data.manager_email }}",
            "text": "{{ trigger.data.name }} joins your team on {{ trigger.data.start_date }}. Please complete the onboarding form.",
        },
    )
    n("join", "logic.merge", "Provisioned", {"mode": "object"})
    n(
        "manager_input",
        "human.request_info",
        "Manager onboarding form",
        {
            "title": "Onboarding details for {{ trigger.data.name }}",
            "approvers": {"role": "approver"},
            "fields": [
                {"name": "buddy", "type": "string", "label": "Onboarding buddy"},
                {"name": "needs_admin_access", "type": "boolean", "label": "Needs admin access?"},
                {"name": "first_project", "type": "text", "required": False},
            ],
            "due_in_hours": 72,
        },
    )
    n(
        "admin_check",
        "logic.if",
        "Admin access requested?",
        {"condition": "{{ nodes.manager_input.output.data.needs_admin_access }}"},
    )
    n(
        "it_approval",
        "human.approval",
        "IT security approval",
        {
            "title": "Grant admin access to {{ trigger.data.name }}?",
            "approvers": {"role": "org_admin"},
            "due_in_hours": 48,
        },
    )
    n(
        "grant",
        "data.http_request",
        "Grant admin role",
        {
            "connection_id": conn("it_api"),
            "method": "POST",
            "url": "/accounts/{{ trigger.data.email }}/roles",
            "body": {"role": "admin"},
        },
    )
    n(
        "welcome_email",
        "comm.email",
        "Welcome email",
        {
            "connection_id": conn("email"),
            "to": ["{{ trigger.data.email }}"],
            "subject": "Welcome to the team!",
            "text": "Hi {{ trigger.data.name }}, your buddy is {{ nodes.manager_input.output.data.buddy }}. See you on {{ trigger.data.start_date }}.",
        },
    )
    n(
        "until_start",
        "logic.wait_until",
        "Wait for start date",
        {"mode": "datetime", "until": "{{ trigger.data.start_date }}T08:00:00Z"},
    )
    n(
        "day_one",
        "comm.email",
        "Day-one reminder",
        {
            "connection_id": conn("email"),
            "to": ["{{ trigger.data.manager_email }}"],
            "subject": "{{ trigger.data.name }} starts today",
            "text": "Laptop ticket: {{ nodes.laptop.output.ticket.key }}",
        },
    )
    t.chain("hired", "fan")
    for i, nid in enumerate(("accounts", "laptop", "folder", "welcome_slack"), start=1):
        t.edge("fan", nid, f"branch_{i}")
        t.edge(nid, "join")
    t.chain("join", "manager_input", "admin_check")
    t.edge("admin_check", "it_approval", "true")
    t.edge("it_approval", "grant", "approved")
    t.edge("manager_input", "welcome_email")
    t.chain("welcome_email", "until_start", "day_one")
    return t


def invoice() -> TemplateSpec:
    t = TemplateSpec(
        slug="invoice-processing",
        name="Invoice Processing",
        category="Finance",
        summary="Extract invoice fields from PDFs with AI, match vendor and PO in the ERP, route exceptions and "
        "high-value invoices to finance approval, post to ERP and record in the ledger.",
        description="Three-way match with AI document extraction.",
        tags=["finance", "ai", "documents", "approval"],
        connection_roles={"ai": "category:ai", "erp": "http_rest", "ledger_db": "postgres", "slack": "slack"},
        variables={"approval_threshold": 10000},
    )
    n = t.node
    n("upload", "trigger.file_uploaded", "Invoice PDF uploaded", {"source": "platform", "filename_pattern": "*.pdf"})
    n(
        "extract",
        "ai.document_extractor",
        "AI Document Extractor",
        {"file_id": "{{ trigger.file.file_id }}", "model": AI},
    )
    n(
        "is_invoice",
        "logic.if",
        "Is a complete invoice?",
        {
            "conditions": [
                {"left": "{{ nodes.extract.output.document_type }}", "operator": "equals", "right": "invoice"},
                {"left": "{{ nodes.extract.output.fields.total }}", "operator": "is_not_empty"},
            ]
        },
    )
    n(
        "vendor",
        "data.http_request",
        "Find vendor in ERP",
        {
            "connection_id": conn("erp"),
            "url": "/vendors",
            "query": {"name": "{{ nodes.extract.output.fields.counterparty_name }}"},
        },
    )
    n(
        "po",
        "data.http_request",
        "Match purchase order",
        {
            "connection_id": conn("erp"),
            "url": "/purchase-orders",
            "query": {
                "vendor": "{{ nodes.extract.output.fields.counterparty_name }}",
                "amount": "{{ nodes.extract.output.fields.total }}",
            },
        },
    )
    n(
        "exception",
        "logic.if",
        "Exception or high value?",
        {
            "condition": "{{ nodes.extract.output.fields.total > vars.approval_threshold or len(default(nodes.po.output.body, [])) == 0 "
            "or nodes.extract.output.confidence < 0.85 }}"
        },
    )
    n(
        "finance",
        "human.approval",
        "Finance approval",
        {
            "title": "Invoice {{ nodes.extract.output.fields.document_number }} — {{ nodes.extract.output.fields.currency }} "
            "{{ nodes.extract.output.fields.total }}",
            "context": "{{ nodes.extract.output }}",
            "approvers": {"role": "approver"},
            "required_approvals": 1,
            "due_in_hours": 72,
        },
    )
    n(
        "post",
        "data.http_request",
        "Post to ERP",
        {
            "connection_id": conn("erp"),
            "method": "POST",
            "url": "/invoices",
            "body": "{{ nodes.extract.output.fields }}",
        },
        retry={"max_attempts": 5, "initial_interval_seconds": 10},
    )
    n(
        "ledger",
        "postgres.query",
        "Record in ledger",
        {
            "connection_id": conn("ledger_db"),
            "sql": "INSERT INTO invoices (number, vendor, total, currency, erp_id) VALUES (:n, :v, :t, :c, :id)",
            "params": {
                "n": "{{ nodes.extract.output.fields.document_number }}",
                "v": "{{ nodes.extract.output.fields.counterparty_name }}",
                "t": "{{ nodes.extract.output.fields.total }}",
                "c": "{{ nodes.extract.output.fields.currency }}",
                "id": "{{ nodes.post.output.body.id }}",
            },
        },
    )
    n(
        "notify",
        "slack.post_message",
        "Notify finance",
        {
            "connection_id": conn("slack"),
            "channel": "#finance",
            "text": "Invoice {{ nodes.extract.output.fields.document_number }} posted.",
        },
    )
    n(
        "manual",
        "slack.post_message",
        "Unreadable document",
        {
            "connection_id": conn("slack"),
            "channel": "#finance",
            "text": "Could not process {{ trigger.file.filename }} — manual entry needed.",
        },
    )
    n("ready", "logic.merge", "Ready to post", {"mode": "list"})
    t.chain("upload", "extract", "is_invoice")
    t.edge("is_invoice", "manual", "false")
    t.edge("is_invoice", "vendor", "true")
    t.chain("vendor", "po", "exception")
    t.edge("exception", "finance", "true")
    t.edge("finance", "ready", "approved")
    t.edge("exception", "ready", "false")
    t.chain("ready", "post", "ledger", "notify")
    return t


def purchase() -> TemplateSpec:
    t = TemplateSpec(
        slug="purchase-approval",
        name="Purchase Approval",
        category="Finance",
        summary="Policy-checked purchase requests with tiered approvals (manager, two directors, CFO) and PO creation.",
        description="AI checks the request against the procurement policy before routing by amount.",
        tags=["procurement", "approval", "ai"],
        connection_roles={"ai": "category:ai", "erp": "http_rest", "email": "capability:email.send"},
    )
    n = t.node
    n("req", "trigger.api_event", "Purchase requested", {"event_name": "purchase.requested"})
    n(
        "policy",
        "ai.decision",
        "Policy check",
        {
            "goal": "Check the purchase request against procurement policy.",
            "policy": "Software over $2k needs security review; travel must be economy; no personal items.",
            "context": "{{ trigger.data }}",
            "options": ["compliant", "non_compliant"],
            "route_by_decision": True,
            "output_schema": {"issues": {"type": "array", "items": {"type": "string"}}},
            "model": AI,
        },
    )
    n(
        "deny",
        "comm.email",
        "Notify non-compliance",
        {
            "connection_id": conn("email"),
            "to": ["{{ trigger.data.requester_email }}"],
            "subject": "Purchase request needs changes",
            "text": "Issues: {{ join(nodes.policy.output.issues, '; ') }}",
        },
    )
    n(
        "tier",
        "logic.switch",
        "Route by amount",
        {
            "value": "{{ trigger.data.amount }}",
            "cases": [
                {"handle": "manager", "operator": "lt", "value": 5000},
                {"handle": "director", "operator": "lt", "value": 50000},
            ],
        },
    )
    n(
        "mgr",
        "human.approval",
        "Manager approval",
        {
            "title": "Purchase ${{ trigger.data.amount }}: {{ trigger.data.item }}",
            "approvers": {"role": "approver"},
            "context": "{{ trigger.data }}",
        },
    )
    n(
        "dirs",
        "human.approval",
        "Two director approvals",
        {
            "title": "Purchase ${{ trigger.data.amount }}: {{ trigger.data.item }}",
            "approvers": {"role": "approver"},
            "required_approvals": 2,
            "context": "{{ trigger.data }}",
        },
    )
    n(
        "cfo",
        "human.approval",
        "CFO approval",
        {
            "title": "Purchase ${{ trigger.data.amount }}: {{ trigger.data.item }}",
            "approvers": {"role": "org_admin"},
            "context": "{{ trigger.data }}",
            "due_in_hours": 120,
        },
    )
    n("approved", "logic.merge", "Approved", {"mode": "list"})
    n(
        "po",
        "data.http_request",
        "Create purchase order",
        {"connection_id": conn("erp"), "method": "POST", "url": "/purchase-orders", "body": "{{ trigger.data }}"},
    )
    n(
        "confirm",
        "comm.email",
        "Confirm to requester",
        {
            "connection_id": conn("email"),
            "to": ["{{ trigger.data.requester_email }}"],
            "subject": "PO created",
            "text": "PO {{ nodes.po.output.body.id }} created.",
        },
    )
    t.chain("req", "policy")
    t.edge("policy", "deny", "non_compliant")
    t.edge("policy", "tier", "compliant")
    t.edge("tier", "mgr", "manager")
    t.edge("tier", "dirs", "director")
    t.edge("tier", "cfo", "default")
    for a in ("mgr", "dirs", "cfo"):
        t.edge(a, "approved", "approved")
    t.chain("approved", "po", "confirm")
    return t


def contract() -> TemplateSpec:
    t = TemplateSpec(
        slug="contract-review",
        name="Contract Review",
        category="Legal",
        summary="AI analyses uploaded contracts for risky clauses, legal reviews high-risk ones, findings are "
        "filed in Jira and Drive.",
        description="",
        tags=["legal", "ai", "documents"],
        connection_roles={
            "ai": "category:ai",
            "tickets": "capability:ticket.create",
            "drive": "google_drive",
            "email": "capability:email.send",
        },
    )
    n = t.node
    n("upload", "trigger.file_uploaded", "Contract uploaded", {"source": "platform", "filename_pattern": "*.pdf"})
    n(
        "analyze",
        "ai.document_analysis",
        "Analyse contract",
        {
            "file_id": "{{ trigger.file.file_id }}",
            "instructions": "Identify parties, term, renewal, liability caps, indemnities, termination rights and unusual clauses.",
            "output_schema": {
                "type": "object",
                "properties": {
                    "parties": {"type": "array", "items": {"type": "string"}},
                    "term": {"type": "string"},
                    "auto_renewal": {"type": "boolean"},
                    "liability_cap": {"type": ["string", "null"]},
                    "risky_clauses": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "clause": {"type": "string"},
                                "risk": {"type": "string", "enum": ["low", "medium", "high"]},
                                "explanation": {"type": "string"},
                            },
                            "required": ["clause", "risk", "explanation"],
                            "additionalProperties": False,
                        },
                    },
                    "overall_risk": {"type": "string", "enum": ["low", "medium", "high"]},
                },
                "required": ["parties", "term", "auto_renewal", "liability_cap", "risky_clauses", "overall_risk"],
                "additionalProperties": False,
            },
            "model": AI,
        },
    )
    n("risky", "logic.if", "High risk?", {"condition": "{{ nodes.analyze.output.data.overall_risk == 'high' }}"})
    n(
        "legal",
        "human.manual_review",
        "Legal review",
        {
            "title": "Review {{ trigger.file.filename }}",
            "context": "{{ nodes.analyze.output.data }}",
            "approvers": {"role": "approver"},
            "editable_fields": {"notes": ""},
            "due_in_hours": 72,
        },
    )
    n(
        "ticket",
        "ticket.create",
        "File legal ticket",
        {
            "connection_id": conn("tickets"),
            "project": "LEGAL",
            "summary": "Contract review: {{ trigger.file.filename }}",
            "description": "{{ json(nodes.analyze.output.data.risky_clauses) }}",
        },
    )
    n(
        "archive",
        "google_drive.upload_file",
        "Archive summary",
        {
            "connection_id": conn("drive"),
            "name": "{{ trigger.file.filename }}.review.json",
            "text_content": "{{ json(nodes.analyze.output.data) }}",
            "mime_type": "application/json",
        },
    )
    n(
        "notify",
        "comm.email",
        "Email summary",
        {
            "connection_id": conn("email"),
            "to": ["legal@acme.example"],
            "subject": "Contract reviewed: {{ trigger.file.filename }}",
            "text": "Overall risk: {{ nodes.analyze.output.data.overall_risk }}",
        },
    )
    t.chain("upload", "analyze", "risky")
    t.edge("risky", "legal", "true")
    t.edge("legal", "ticket", "approved")
    t.edge("analyze", "archive")
    t.edge("archive", "notify")
    return t


def churn() -> TemplateSpec:
    t = TemplateSpec(
        slug="customer-churn-alert",
        name="Customer Churn Alert",
        category="Customer Success",
        summary="Nightly scan of usage data, AI churn-risk assessment per account, CSM tasks and Slack alerts for "
        "high-risk accounts.",
        description="",
        tags=["customer-success", "ai", "loop", "schedule"],
        connection_roles={
            "ai": "category:ai",
            "warehouse": "postgres",
            "crm": "capability:crm.create_task",
            "slack": "slack",
        },
    )
    n = t.node
    n("nightly", "trigger.schedule", "Every night at 02:00", {"cron": "0 2 * * *", "timezone": "UTC"})
    n(
        "accounts",
        "postgres.query",
        "Accounts with declining usage",
        {
            "connection_id": conn("warehouse"),
            "sql": "SELECT account_id, name, csm_email, contact_id, usage_trend, open_tickets, nps FROM account_health "
            "WHERE usage_trend < -0.2 ORDER BY usage_trend LIMIT 200",
        },
    )
    n("each", "logic.loop", "For each account", {"items": "{{ nodes.accounts.output.rows }}", "max_concurrency": 5})
    n(
        "assess",
        "ai.decision",
        "Assess churn risk",
        {
            "goal": "Assess the churn risk of this account.",
            "context": "{{ loop.item }}",
            "options": ["high", "medium", "low"],
            "output_schema": {"recommended_action": {"type": "string"}},
            "model": AI,
        },
    )
    n("high", "logic.if", "High risk?", {"condition": "{{ nodes.assess.output.decision == 'high' }}"})
    n(
        "task",
        "crm.create_task",
        "CSM task",
        {
            "connection_id": conn("crm"),
            "contact_id": "{{ loop.item.contact_id }}",
            "subject": "Churn risk: {{ loop.item.name }}",
            "body": "{{ nodes.assess.output.recommended_action }}",
            "priority": "HIGH",
        },
    )
    n(
        "alert",
        "slack.post_message",
        "Alert CS channel",
        {
            "connection_id": conn("slack"),
            "channel": "#churn-alerts",
            "text": ":warning: {{ loop.item.name }} — {{ nodes.assess.output.reasoning_summary }}",
        },
    )
    n(
        "report",
        "slack.post_message",
        "Daily summary",
        {
            "connection_id": conn("slack"),
            "channel": "#churn-alerts",
            "text": "Churn scan done: {{ nodes.each.output.count }} accounts assessed, "
            "{{ len([r for r in nodes.each.output.results if r]) }} flagged.",
        },
    )
    t.chain("nightly", "accounts", "each")
    t.edge("each", "assess", "body")
    t.chain("assess", "high")
    t.edge("high", "task", "true")
    t.edge("task", "alert")
    t.edge("each", "report", "done")
    return t


def recruiting() -> TemplateSpec:
    t = TemplateSpec(
        slug="recruitment-candidate-screening",
        name="Recruitment Candidate Screening",
        category="HR",
        summary="Parse resumes, extract skills with AI, score fit against the role, shortlist/reject automatically "
        "and ask recruiters about borderline candidates.",
        description="",
        tags=["hr", "ai", "documents"],
        connection_roles={"ai": "category:ai", "email": "capability:email.send", "ats": "http_rest"},
    )
    n = t.node
    n("applied", "trigger.webhook", "Application received (ATS)", {"authentication": "signature"})
    n("resume", "data.file_read", "Read resume", {"file_id": "{{ trigger.body.resume_file_id }}"})
    n(
        "profile",
        "ai.extract",
        "Extract candidate profile",
        {
            "text": "{{ nodes.resume.output.text }}",
            "fields": [
                {"name": "skills", "type": "array"},
                {"name": "years_experience", "type": "number"},
                {"name": "current_title", "type": "string"},
                {"name": "location", "type": "string"},
            ],
            "model": AI,
        },
    )
    n(
        "fit",
        "ai.decision",
        "Score role fit",
        {
            "goal": "Decide whether to shortlist the candidate for the role.",
            "policy": "Assess only job-relevant skills and "
            "experience. Ignore age, gender, ethnicity, nationality and other protected attributes.",
            "context": {"role": "{{ trigger.body.job }}", "candidate": "{{ nodes.profile.output.data }}"},
            "options": ["shortlist", "maybe", "reject"],
            "route_by_decision": True,
            "output_schema": {"fit_score": {"type": "number", "minimum": 0, "maximum": 100}},
            "model": AI,
        },
    )
    n(
        "recruiter",
        "human.manual_review",
        "Recruiter decision",
        {
            "title": "Borderline candidate: {{ trigger.body.name }}",
            "context": "{{ nodes.fit.output }}",
            "approvers": {"role": "approver"},
            "due_in_hours": 48,
        },
    )
    n(
        "invite",
        "comm.email",
        "Interview invitation",
        {
            "connection_id": conn("email"),
            "to": ["{{ trigger.body.email }}"],
            "subject": "Next steps for {{ trigger.body.job.title }}",
            "text": "Hi {{ trigger.body.name }}, we'd like to schedule an interview.",
        },
    )
    n(
        "decline",
        "comm.email",
        "Polite decline",
        {
            "connection_id": conn("email"),
            "to": ["{{ trigger.body.email }}"],
            "subject": "Your application",
            "text": "Thank you for applying. We will not proceed at this time.",
        },
    )
    n(
        "update_ats",
        "data.http_request",
        "Update ATS",
        {
            "connection_id": conn("ats"),
            "method": "PATCH",
            "url": "/candidates/{{ trigger.body.candidate_id }}",
            "body": {"fit_score": "{{ nodes.fit.output.fit_score }}", "decision": "{{ nodes.fit.output.decision }}"},
        },
    )
    n("yes", "logic.merge", "Proceed", {"mode": "list"})
    n("no", "logic.merge", "Decline", {"mode": "list"})
    t.chain("applied", "resume", "profile", "fit")
    t.edge("fit", "yes", "shortlist")
    t.edge("fit", "recruiter", "maybe")
    t.edge("fit", "no", "reject")
    t.edge("recruiter", "yes", "approved")
    t.edge("recruiter", "no", "rejected")
    t.edge("yes", "invite")
    t.edge("no", "decline")
    t.edge("yes", "update_ats")
    t.edge("no", "update_ats")
    return t


def incident() -> TemplateSpec:
    t = TemplateSpec(
        slug="incident-escalation",
        name="Incident Escalation",
        category="IT Operations",
        summary="Classify monitoring alerts, open P1 tickets, page on-call via SMS, alert Slack/Teams and escalate "
        "to the manager if not acknowledged in 15 minutes.",
        description="",
        tags=["itops", "sms", "escalation"],
        connection_roles={
            "ai": "category:ai",
            "tickets": "capability:ticket.create",
            "slack": "slack",
            "teams": "teams",
            "sms": "capability:sms.send",
        },
        variables={"oncall_phone": "+15550100100", "manager_phone": "+15550100200"},
    )
    n = t.node
    n(
        "alert",
        "trigger.webhook",
        "Monitoring alert",
        {"authentication": "signature", "correlation_key": "{{ body.alert_id }}"},
    )
    n(
        "severity",
        "ai.classify",
        "Classify severity",
        {
            "text": "{{ json(trigger.body) }}",
            "route_by_category": True,
            "categories": [
                {"name": "sev1", "description": "Customer-facing outage or data loss"},
                {"name": "sev2", "description": "Degraded service"},
                {"name": "sev3", "description": "Minor or noise"},
            ],
            "model": AI,
        },
    )
    n(
        "ticket",
        "ticket.create",
        "Open P1 incident",
        {
            "connection_id": conn("tickets"),
            "project": "OPS",
            "priority": "Highest",
            "summary": "[SEV1] {{ trigger.body.title }}",
            "description": "{{ json(trigger.body) }}",
        },
    )
    n(
        "page",
        "comm.sms",
        "Page on-call",
        {
            "connection_id": conn("sms"),
            "to": "{{ vars.oncall_phone }}",
            "body": "SEV1: {{ trigger.body.title }} — ack with code {{ trigger.body.alert_id }}",
        },
    )
    n(
        "slack",
        "slack.post_message",
        "Post to #incidents",
        {
            "connection_id": conn("slack"),
            "channel": "#incidents",
            "text": ":fire: SEV1 {{ trigger.body.title }} ({{ nodes.ticket.output.ticket.key }})",
        },
    )
    n(
        "teams",
        "teams.post_message",
        "Post to Teams",
        {
            "connection_id": conn("teams"),
            "title": "SEV1 incident",
            "text": "{{ trigger.body.title }}",
            "facts": {"Ticket": "{{ nodes.ticket.output.ticket.key }}"},
        },
    )
    n(
        "ack",
        "logic.wait_until",
        "Wait for acknowledgement",
        {
            "mode": "event",
            "event_key": "incident-ack:{{ trigger.body.alert_id }}",
            "timeout": 15,
            "timeout_unit": "minutes",
        },
    )
    n(
        "escalate",
        "comm.sms",
        "Escalate to manager",
        {
            "connection_id": conn("sms"),
            "to": "{{ vars.manager_phone }}",
            "body": "UNACKED SEV1 for 15m: {{ trigger.body.title }}",
        },
    )
    n(
        "minor",
        "slack.post_message",
        "Log lower-severity alert",
        {
            "connection_id": conn("slack"),
            "channel": "#alerts",
            "text": "{{ nodes.severity.output.category }}: {{ trigger.body.title }}",
        },
    )
    t.chain("alert", "severity")
    t.edge("severity", "ticket", "sev1")
    t.edge("severity", "minor", "sev2")
    t.edge("severity", "minor", "sev3")
    t.edge("ticket", "page")
    t.edge("ticket", "slack")
    t.edge("ticket", "teams")
    t.edge("page", "ack")
    t.edge("ack", "escalate", "timeout")
    return t


def exec_report() -> TemplateSpec:
    t = TemplateSpec(
        slug="automated-executive-reporting",
        name="Automated Executive Reporting",
        category="Operations",
        summary="Weekly KPI extraction from the warehouse, AI executive narrative, Excel workbook to Drive and "
        "delivery by email and Teams.",
        description="",
        tags=["reporting", "ai", "schedule", "excel"],
        connection_roles={
            "ai": "category:ai",
            "warehouse": "postgres",
            "drive": "google_drive",
            "email": "capability:email.send",
            "teams": "teams",
        },
    )
    n = t.node
    n("weekly", "trigger.schedule", "Mondays 07:00", {"cron": "0 7 * * 1", "timezone": "America/New_York"})
    n(
        "revenue",
        "postgres.query",
        "Revenue KPIs",
        {
            "connection_id": conn("warehouse"),
            "sql": "SELECT week, region, revenue, new_customers FROM weekly_revenue ORDER BY week DESC LIMIT 20",
        },
    )
    n(
        "support",
        "postgres.query",
        "Support KPIs",
        {
            "connection_id": conn("warehouse"),
            "sql": "SELECT week, tickets, csat, median_first_response_min FROM weekly_support ORDER BY week DESC LIMIT 8",
        },
    )
    n("both", "logic.merge", "KPIs", {"mode": "object"})
    n(
        "narrative",
        "ai.summarize",
        "Executive narrative",
        {
            "text": "Revenue: {{ json(nodes.revenue.output.rows) }}\nSupport: {{ json(nodes.support.output.rows) }}",
            "style": "executive",
            "max_words": 250,
            "focus": "week-over-week changes, risks and wins",
            "model": AI,
        },
    )
    n(
        "xlsx",
        "data.excel",
        "Build workbook",
        {
            "operation": "write",
            "rows": "{{ nodes.revenue.output.rows }}",
            "filename": "exec-report-{{ format_date(now(), '%Y-%m-%d') }}.xlsx",
        },
    )
    n(
        "upload",
        "google_drive.upload_file",
        "Upload to Drive",
        {
            "connection_id": conn("drive"),
            "name": "{{ nodes.xlsx.output.file.filename }}",
            "platform_file_id": "{{ nodes.xlsx.output.file.file_id }}",
        },
    )
    n(
        "email",
        "comm.email",
        "Email executives",
        {
            "connection_id": conn("email"),
            "to": ["exec-team@acme.example"],
            "subject": "Weekly business review",
            "text": "{{ nodes.narrative.output.summary }}\n\nWorkbook: {{ nodes.upload.output.web_view_link }}",
        },
    )
    n(
        "teams",
        "teams.post_message",
        "Post to leadership channel",
        {
            "connection_id": conn("teams"),
            "title": "Weekly business review",
            "text": "{{ nodes.narrative.output.summary }}",
        },
    )
    t.chain("weekly", "revenue")
    t.edge("weekly", "support")
    t.edge("revenue", "both")
    t.edge("support", "both")
    t.chain("both", "narrative")
    t.edge("both", "xlsx")
    t.edge("xlsx", "upload")
    t.edge("narrative", "email")
    t.edge("upload", "email")
    t.edge("narrative", "teams")
    return t


def all_templates() -> list[TemplateSpec]:
    return [
        customer_support(),
        sales_lead(),
        onboarding(),
        invoice(),
        purchase(),
        contract(),
        churn(),
        recruiting(),
        incident(),
        exec_report(),
    ]
