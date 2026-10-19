# Optimizer Calibration & Dual-Lens Judge UI Layout

## Overview

This document describes the responsive layout architecture for the Resume Optimizer's Run Status card, with specific focus on fixing vertical screen / portrait monitor rendering where nested multi-column grids caused extreme vertical distortion, cramped evaluation cards, and expansive dead whitespace.

---

## 1. Problem Identification

### Symptoms in Vertical/Portrait Screens (e.g. 1080x1920)
In vertical screens or portrait monitor viewports (~900px–1080px wide by 1600px–1920px tall):
1. **Outer 50/50 Grid Mismatch (`lg:grid-cols-2`):**
   - The outer container split the page into two equal ~480px columns.
   - The left column housed only a single circular ATS match dial (144px) and 3 lines of text (~200px tall).
   - The right column housed the full "Signals from judges" evaluation report.
2. **Inner Column Squeeze (`md:grid-cols-2`):**
   - Inside the right 480px column, the ATS Searchability and Recruiter Review cards were split *again* into two sub-columns (`md:grid-cols-2` since viewport width >= 768px).
   - This squeezed each evaluation card into barely ~230px of width (only 25% of total screen width).
3. **Severe Vertical Ballooning & Whitespace:**
   - At 230px width, lengthy critique text, monospace code snippets (`"Provided architectural leadership..."`), and narrative feedback wrapped every few words, stretching the Recruiter Review card to over 1000px tall.
   - CSS grid row stretching forced the ATS card to match this 1000px height, creating ~500px of empty space inside the ATS card border.
   - The left column was stretched to match the right column, leaving ~800px of empty white void on the left half of the screen.
   - The **Dual-Pane Diff Workbench** / draft editor was pushed >1400px down, requiring extensive vertical scrolling to access.

---

## 2. Architectural Redesign

The layout was restructured in both the server-rendered template ([`_optimizer_status_inner.html`](file:///D:/Workshop/JobApp-Main/django_project/resume_app/templates/resume_app/_optimizer_status_inner.html)) and the reactive polling JS renderer ([`optimizer.html`](file:///D:/Workshop/JobApp-Main/django_project/resume_app/templates/resume_app/optimizer.html)).

### Before (Nested Split Antipattern)
```
┌───────────────────────────────────┬───────────────────────────────────┐
│ Status: Scoring...                │ Signals from judges [Comp: 78]    │
│ (ATS Dial - 144px)                ├─────────────────┬─────────────────┤
│ Recruiter: 88                     │ ATS (25% width) │ Recruiter (25%) │
│ Composite: 78                     │                 │                 │
│                                   │ (empty bottom)  │ (stretched to   │
│ [~800px dead white void]          │                 │  1000px height) │
└───────────────────────────────────┴─────────────────┴─────────────────┘
```

### After (Executive Header Banner + Full-Width Judges Section)
```
┌───────────────────────────────────────────────────────────────────────┐
│  Calibration & Run Status Overview Banner                             │
│  ┌──────────────┐  ┌────────────────────────────────────────────────┐ │
│  │   ATS Dial   │  │ Status: Scoring [Live pulse] | Tokens: 0 in/out│ │
│  │   (128px)    │  │ ┌──────────────────────┐┌────────────────────┐ │ │
│  │   90 Match   │  │ │ Recruiter Score: 88  ││ Composite: 78/100  │ │ │
│  │              │  │ └──────────────────────┘└────────────────────┘ │ │
│  └──────────────┘  └────────────────────────────────────────────────┘ │
└───────────────────────────────────────────────────────────────────────┘
┌───────────────────────────────────────────────────────────────────────┐
│ Signals from Judges [Composite: 78/100]                               │
│ [ Priority Revisions for Next Pass - Full Width Banner ]              │
│ ┌──────────────────────────────────┬────────────────────────────────┐ │
│ │ ATS Searchability (50% width)    │ Recruiter Review (50% width)   │ │
│ │ - Clean keyword chips            │ - Wide phrasing critique cards │ │
│ │ - Full-width parseability alerts │ - Formatted snippet quotes     │ │
│ │ - Legible placement directives   │ - Balanced 450px height        │ │
│ └──────────────────────────────────┴────────────────────────────────┘ │
└───────────────────────────────────────────────────────────────────────┘
```

---

## 3. Key Responsive Improvements

1. **Top Overview Banner:**
   - Circular gauge dial sized to `h-32 w-32` (128px) placed alongside a live status pill, token counter, and collapsible context budget.
   - Two dedicated KPI cards for **Recruiter Score** (`88/100`) and **Composite Calibration** (`78/100`) make all calibration benchmarks visible at a glance without reading tiny subordinate labels.
2. **Generous Dual-Lens Grid:**
   - Signals from Judges now occupies the full container width.
   - On screens `>= 768px` (`md:grid-cols-2`), each evaluation card receives ~480px width on vertical monitors and ~600px on desktop monitors.
   - Code snippets, badges, and bullet critiques fit on 1–2 lines instead of wrapping into fragmented text columns.
   - Recruiter Review card height is cut by more than 50% (~450px vs ~1000px), perfectly balancing the ATS card and eliminating awkward interior whitespace.
3. **Screen Real Estate Efficiency:**
   - Total status section height is reduced from >1500px to ~650px.
   - Users on vertical monitors immediately see both their multi-judge scores and the resume diff workbench without scrolling through empty space.
4. **Mobile & Tablet Resiliency:**
   - On smaller screens (`< 768px`), cards stack cleanly into single-column cards (`grid-cols-1`) with no horizontal clipping.
