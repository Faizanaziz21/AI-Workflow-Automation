using System;
using System.Linq;
using System.Net.Http;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using System.Threading.Tasks;

namespace AiWorkflowAutomation;

/// <summary>Single-turn LLM completion. Workflow steps build a prompt and parse the reply.</summary>
public interface ILlm
{
    Task<string> CompleteAsync(string system, string user);
}

/// <summary>Real backend: Anthropic Messages API. Reads ANTHROPIC_API_KEY; defaults to claude-sonnet-5-5.</summary>
public sealed class AnthropicLlm : ILlm
{
    private static readonly HttpClient Http = new();
    private readonly string _key;
    private readonly string _model;

    public AnthropicLlm(string? model = null)
    {
        _key = Environment.GetEnvironmentVariable("ANTHROPIC_API_KEY")
               ?? throw new InvalidOperationException("ANTHROPIC_API_KEY is not set.");
        _model = model ?? "claude-sonnet-5-5";
    }

    public async Task<string> CompleteAsync(string system, string user)
    {
        var body = new
        {
            model = _model,
            max_tokens = 512,
            system,
            messages = new[] { new { role = "user", content = user } }
        };
        using var req = new HttpRequestMessage(HttpMethod.Post, "https://api.anthropic.com/v1/messages")
        {
            Content = new StringContent(JsonSerializer.Serialize(body), Encoding.UTF8, "application/json")
        };
        req.Headers.Add("x-api-key", _key);
        req.Headers.Add("anthropic-version", "2023-06-01");

        using var resp = await Http.SendAsync(req);
        string json = await resp.Content.ReadAsStringAsync();
        if (!resp.IsSuccessStatusCode) throw new HttpRequestException($"Anthropic API {(int)resp.StatusCode}: {json}");

        using var doc = JsonDocument.Parse(json);
        return doc.RootElement.GetProperty("content")[0].GetProperty("text").GetString() ?? "";
    }
}

/// <summary>
/// Deterministic offline stand-in so the workflow runs without an API key. It recognises what a
/// step is asking (classify / extract / draft) from the system prompt and produces a sensible
/// rule-based answer from the ticket text. A harness for the demo — not an LLM.
/// </summary>
public sealed class MockLlm : ILlm
{
    public Task<string> CompleteAsync(string system, string user)
    {
        string s = system.ToLowerInvariant();
        string text = user;

        if (s.Contains("classify"))
            return Task.FromResult($$"""{"category":"{{Category(text)}}","urgency":"{{Urgency(text)}}"}""");

        if (s.Contains("extract"))
        {
            string order = Match(text, @"#(\d{3,})", "order_id");
            string err = Match(text, @"(0x[0-9A-Fa-f]{4,})", "error_code");
            string plan = Regex.IsMatch(text, @"\bpro\b", RegexOptions.IgnoreCase) ? "\"plan\":\"Pro\"" : "";
            var parts = new[] { order, err, plan }.Where(p => p.Length > 0);
            return Task.FromResult("{" + string.Join(",", parts) + "}");
        }

        if (s.Contains("draft") || s.Contains("reply"))
        {
            string cat = Category(text);
            string line = cat switch
            {
                "Billing"   => "I've flagged your billing/refund issue for our finance team and we'll confirm the status shortly.",
                "Technical" => "Thanks for the details — our engineering team will investigate the error and follow up.",
                "Account"   => "I've triggered a fresh password-reset link; please also check your spam folder.",
                _           => "Thanks for your question — a specialist will get back to you with details."
            };
            return Task.FromResult($"Hi,\n\nThanks for reaching out. {line}\n\nBest regards,\nSupport Team");
        }

        return Task.FromResult("(mock: unrecognised step)");
    }

    private static string Category(string t)
    {
        t = t.ToLowerInvariant();
        if (Regex.IsMatch(t, @"refund|charge|billing|invoice|payment|discount|pric")) return "Billing";
        if (Regex.IsMatch(t, @"crash|error|bug|0x|not working|broken|fail")) return "Technical";
        if (Regex.IsMatch(t, @"log ?in|password|locked|account|reset")) return "Account";
        return "General";
    }

    private static string Urgency(string t)
    {
        t = t.ToLowerInvariant();
        if (Regex.IsMatch(t, @"urgent|asap|immediately|locked out|can't|cannot|down|weeks|yesterday")) return "High";
        if (Regex.IsMatch(t, @"no rush|whenever|question|curious")) return "Low";
        return "Medium";
    }

    private static string Match(string text, string pattern, string key)
    {
        var m = Regex.Match(text, pattern);
        return m.Success ? $"\"{key}\":\"{m.Groups[1].Value}\"" : "";
    }
}
