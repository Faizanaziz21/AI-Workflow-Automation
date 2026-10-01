using System.Text.Json;
using System.Threading.Tasks;

namespace AiWorkflowAutomation;

/// <summary>Helpers shared by the LLM-backed steps.</summary>
internal static class Json
{
    /// <summary>Pulls the first {...} object out of a model reply and returns a specific field, or "".</summary>
    public static string Field(string reply, string key)
    {
        int a = reply.IndexOf('{'), b = reply.LastIndexOf('}');
        if (a < 0 || b <= a) return "";
        try
        {
            using var doc = JsonDocument.Parse(reply.Substring(a, b - a + 1));
            return doc.RootElement.TryGetProperty(key, out var v) ? v.ToString() : "";
        }
        catch { return ""; }
    }
}

/// <summary>Step 1 — classify the ticket into a category and urgency.</summary>
public sealed class ClassifyStep(ILlm llm) : IWorkflowStep
{
    public string Name => "classify";
    public async Task RunAsync(WorkflowContext ctx)
    {
        const string sys = "You classify support tickets. Respond ONLY as compact JSON "
                         + "{\"category\":\"...\",\"urgency\":\"...\"}. "
                         + "category is one of Billing, Technical, Account, General; urgency is Low, Medium, or High.";
        var reply = await llm.CompleteAsync(sys, $"{ctx.Ticket.Subject}\n\n{ctx.Ticket.Body}");
        ctx.Data["category"] = Json.Field(reply, "category") is { Length: > 0 } c ? c : "General";
        ctx.Data["urgency"] = Json.Field(reply, "urgency") is { Length: > 0 } u ? u : "Medium";
    }
}

/// <summary>Step 2 — extract key structured fields from the ticket.</summary>
public sealed class ExtractStep(ILlm llm) : IWorkflowStep
{
    public string Name => "extract";
    public async Task RunAsync(WorkflowContext ctx)
    {
        const string sys = "Extract key fields from the support ticket as compact JSON. "
                         + "Include only fields present: order_id, error_code, plan.";
        var reply = await llm.CompleteAsync(sys, ctx.Ticket.Body);
        int a = reply.IndexOf('{'), b = reply.LastIndexOf('}');
        ctx.Data["extracted"] = (a >= 0 && b > a) ? reply.Substring(a, b - a + 1) : "{}";
    }
}

/// <summary>Step 3 — draft a reply to the customer.</summary>
public sealed class DraftReplyStep(ILlm llm) : IWorkflowStep
{
    public string Name => "draft";
    public async Task RunAsync(WorkflowContext ctx)
    {
        const string sys = "You draft a short, friendly support reply (3-4 sentences). No placeholders.";
        var user = $"Ticket: {ctx.Ticket.Subject}\n{ctx.Ticket.Body}\nCategory: {ctx.Data.GetValueOrDefault("category")}";
        ctx.Data["reply"] = (await llm.CompleteAsync(sys, user)).Trim();
    }
}

/// <summary>Step 4 — route the ticket to a queue (rule-based, no LLM).</summary>
public sealed class RouteStep : IWorkflowStep
{
    public string Name => "route";
    public Task RunAsync(WorkflowContext ctx)
    {
        string category = ctx.Data.GetValueOrDefault("category", "General");
        string urgency = ctx.Data.GetValueOrDefault("urgency", "Medium");
        ctx.Data["route"] = urgency == "High" ? $"PRIORITY → {category} team" : $"{category} team";
        return Task.CompletedTask;
    }
}
