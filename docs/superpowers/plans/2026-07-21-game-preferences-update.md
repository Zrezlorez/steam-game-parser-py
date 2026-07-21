# Game Preferences Update Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Incorporate the latest played, wishlist, and rejected-game evidence into the existing personal recommendation profile.

**Architecture:** Edit only `../Мои игровые предпочтения.md`. Preserve its progression-centered structure and add concise evidence about manual-to-automated development, content depth, goals, visual/UI quality, originality, and qualified roguelike preferences.

**Tech Stack:** Russian Markdown/Obsidian note.

## Global Constraints

- Keep the first-person voice and existing useful links.
- Distinguish played-and-liked games from wishlist interest.
- Do not turn 2D presentation or the roguelike genre into absolute exclusions.
- Group rejected games by reason instead of creating a repetitive blacklist.
- Use neutral durable wording rather than copying conversational profanity.

---

### Task 1: Update and verify the preference profile

**Files:**
- Modify: `../Мои игровые предпочтения.md`

**Interfaces:**
- Consumes: the existing preference note and the approved design spec.
- Produces: a self-contained human-readable profile for future game recommendations.

- [ ] **Step 1: Update the preference rules**

Add the following evidence to the existing sections without changing their overall order:

- the preferred loop `manual work → upgrades → functional base/production → automation → sale or reinvestment`;
- content volume and continued late-game variety;
- a requirement for visible direction, with aimless sandbox play and overloaded interfaces as negatives;
- visual coherence, readable UI, and originality as recommendation criteria;
- generic roguelikes as low-priority unless unusually polished or mechanically distinctive;
- cheap or derivative 2D presentation as a negative, while explicitly retaining Mindustry as a counterexample.

- [ ] **Step 2: Add examples with evidence strength**

Add Schedule I, Hydroneer, No Man's Sky, and Hades II under played evidence with the user's reasons. Add Captain of Industry, Mindustry, and Ctrl Alt Ego under a separate wishlist-interest label. Group the rejected games under visual/UI, derivative design, grind, and generic-roguelike reasons.

- [ ] **Step 3: Update the recommendation checklist**

Expand the five-question checklist only enough to ask whether the game has sustained content, clear direction, readable presentation, and an original reason to choose it over stronger genre references. Keep the existing three-level result interpretation.

- [ ] **Step 4: Verify the note**

Run:

```powershell
rg -n "Schedule I|Hydroneer|No Man's Sky|Hades II|Captain of Industry|Mindustry|Ctrl Alt Ego|визуал|интерфейс|рогал" "..\Мои игровые предпочтения.md"
```

Expected: every new positive example appears; the note explicitly qualifies visual and roguelike preferences.

Read the full note once and confirm it contains no absolute rejection of all 2D games or all roguelikes, no duplicated sections, and no contradiction between Mindustry/Hades II and the negative examples.
