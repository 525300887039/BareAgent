# Backend Development Guidelines

> Best practices for backend development in this project.

---

## Overview

This directory contains guidelines for backend development. Fill in each file with your project's specific conventions.

---

## Guidelines Index

| Guide | Description | Status |
|-------|-------------|--------|
| [Directory Structure](./directory-structure.md) | `src/` layout and where new code goes | Filled |
| [State & Persistence](./state-persistence.md) | JSONL mailboxes, atomic JSON, session IDs, config layering | Filled |
| [Database Guidelines](./database-guidelines.md) | (BareAgent has no database — see `state-persistence.md`) | Redirect |
| [Error Handling](./error-handling.md) | Custom exceptions, fail-closed permissions, structured tool errors | Filled |
| [Logging Guidelines](./logging-guidelines.md) | `AgentConsole` / `StreamPrinter` / `InteractionLogger` / `tracer` (not `logging`) | Filled |
| [Quality Guidelines](./quality-guidelines.md) | Python 3.12+, ruff, pytest, Conventional Commits, anti-over-engineering | Filled |

---

## How to Fill These Guidelines

For each guideline file:

1. Document your project's **actual conventions** (not ideals)
2. Include **code examples** from your codebase
3. List **forbidden patterns** and why
4. Add **common mistakes** your team has made

The goal is to help AI assistants and new team members understand how YOUR project works.

---

**Language**: All documentation should be written in **English**.
