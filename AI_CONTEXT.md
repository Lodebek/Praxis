# Praxis AI Context & Rules

**CRITICAL INSTRUCTIONS FOR ANY AI ASSISTANT WORKING ON THIS REPOSITORY:**
Read this entire document before executing any commands or editing any files.

## 1. Git Identity & Boundaries (STRICT)
- **NEVER use the global Git configuration.** The host machine has a work email globally configured (`john@sandpointsystems.com`). That email must NEVER touch this repository.
- **Always use the local Git configuration.** This project is strictly bound to the personal GitHub account. The local config is already set to the `Lodebek` GitHub `noreply` email. 
- **NEVER run `git filter-branch` or `git push --force`** without explicit user consent and a deep diagnostic check of the remote state. 
- **Do not automatically add AI contributor tags** (e.g., `Co-Authored-By: Claude`) to commit messages unless explicitly requested.

## 2. Documentation Rules
- **Never condense or "hack" the README.** This user values expansive, hyper-detailed, obsessive mechanical depth. 
- **Maintain consistency:** Praxis supports 4 media types (Movies, TV Shows, PC Games, E-Books). Any feature added to one must be logically consistent with the others. Do not use random bolding or imbalanced typography.
- **Privacy Optics:** Do not write documentation that makes the app sound like malware (e.g., "mines your folders") or an API spammer. Emphasize that it is a "local-first lightweight hub" and uses "intelligent batching" for public APIs (Steam, Google Books). Keep piracy-related regex logic (e.g., stripping `[GOG]`, `Repack`) completely out of public documentation.

## 3. Communication Protocol
- **Do not blindly commit or push code.**
- If you are about to change the README or any critical file, output the *exact text* of the change in your chat response and wait for explicit approval before running `git commit`.
- If you run into a conflict or issue, stop and explain the exact mechanical failure before trying to fix it automatically.

## 4. Architecture Summary
- **Media Types:** Movies, TV Shows, PC Games, E-Books.
- **External APIs:** Plex (local), TMDB (Movies/TV), Steam (Games), Google Books (Books), OpenRouter (LLM).
- **Core Loop:** Users rate media (👍👍, 👍, 👎) and provide 1-line notes. This feeds an exclusion list and a unified taste profile, which is sent as a prompt to OpenRouter to return hyper-personalized recommendations via interactive cards.
- **Tech Stack:** Python 3.11+, FastAPI, SQLite (`data/praxis.db`), Vanilla JS/CSS. No build step. No Node.js.
