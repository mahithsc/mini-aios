from agent import create_agent

DREAM_PROMPT = """\
You are now dreaming. Your role has shifted from building what the user asks for \
to reflecting on the conversations you've had. Research the new chat sessions and \
create reusable skills from what you find.

Chat sessions live in session/session_manifest.json. Focus on chats marked "new" -- \
everything else has already been dreamed on. The actual transcripts are in the \
session/ folder. Once you've processed a chat, mark it "dreamed" in the manifest.

If a conversation has something worth remembering, write a skill file to skills/ \
and register it in skills/skills_index.json. See skills/nano_banana.md for what a \
good skill file looks like. Each index entry should have: id, title, summary, file.

Not every conversation is worth a skill. Use your judgment.
Do not ask follow up questions when dreaming. The user cannot answer.
"""

def dream():
    print("Dreaming...\n")
    dream_agent = create_agent()
    response = dream_agent.run(DREAM_PROMPT, stream=True, stream_events=True)
    for event in response:
        if event.event == "RunContent":
            if event.content is not None:
                print(event.content, end="", flush=True)
    print("\n\nDone dreaming.")