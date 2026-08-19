"""Describe screenshots via z.ai vision models (GLM-4.5V / glm-4v). No X calls."""
import base64
import sys
import httpx

KEY = open(r"D:\ai\openstanley\.env", encoding="utf-8").read().split("OPENSTANLEY_LLM_API_KEY=")[1].splitlines()[0].strip()
URL = "https://api.z.ai/api/anthropic"

PROMPT = """You are looking at a screenshot of getstanley.ai (an AI content agent for X/Twitter), page x.getstanley.ai/write.
Describe IN FULL DETAIL for a developer who must clone this UI:
1. Overall layout: regions, panels, sidebar, columns, their approximate proportions.
2. Every visible UI element: buttons, tabs, menus, input fields, cards, lists — with their exact labels/text.
3. Color scheme: background colors, accent colors, text colors (approx hex guesses).
4. Typography & spacing style (rounded corners, dense/airy, light/dark theme).
5. What the main workflow appears to be (what does the user do on this screen?).
Be exhaustive and concrete. Plain text, structured with headers."""

def describe(path: str, model: str) -> str:
    b64 = base64.b64encode(open(path, "rb").read()).decode()
    body = {
        "model": model,
        "max_tokens": 2000,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
                {"type": "text", "text": PROMPT},
            ],
        }],
    }
    r = httpx.post(URL, headers={"x-api-key": KEY, "anthropic-version": "2023-06-01"},
                   json=body, timeout=120)
    r.raise_for_status()
    return r.json()["content"][0]["text"]

if __name__ == "__main__":
    imgs = [
        r"C:\Users\Fadinl\AppData\Local\hermes\cache\images\img_692fee3e69a2.jpg",
        r"C:\Users\Fadinl\AppData\Local\hermes\cache\images\img_c56ad08575ee.jpg",
        r"C:\Users\Fadinl\AppData\Local\hermes\cache\images\img_465a6c2a4b32.jpg",
    ]
    model = sys.argv[1] if len(sys.argv) > 1 else "glm-4.5v"
    for p in imgs:
        try:
            out = describe(p, model)
            name = p.split("\\")[-1].split(".")[0]
            open(rf"D:\ai\openstanley\docs\references\openstanley-ui-{name}.txt", "w", encoding="utf-8").write(out)
            print(f"=== {name} ({model}) OK, {len(out)} chars saved ===")
        except Exception as e:
            print(f"=== {name} ({model}) FAILED: {str(e)[:200]}")
