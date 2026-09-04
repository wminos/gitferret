# Agent Guidelines & Workflow Rules

## Commit Discipline

Every completed unit of work must follow strict commit discipline.

### 1. Timing & Workflow
- **Commit Trigger**: When a task, feature, fix, or refactoring is completed and verified (syntax check/tests pass), create a git commit immediately.
- **Verification First**: Always run validation (e.g. `python3 -m py_compile ...` or tests) prior to committing. Never commit broken code.
- **Atomic Commits**: Keep commits atomic and focused on a single logical change. Do not bundle unrelated changes together.
- **Selective Staging**: Explicitly stage modified/created project files. Never stage temporary scratch files, untracked artifacts, or unrelated local edits.

### 2. Commit Message Convention
Follow the **Conventional Commits** specification:

```text
<type>: <description>
```

#### Types
- `feat`: New feature or capability
- `fix`: Bug fix or error resolution
- `refactor`: Code change that neither fixes a bug nor adds a feature
- `style`: Formatting, missing semi-colons, whitespace, etc.
- `chore`: Build tools, dependencies, configs, or maintenance tasks
- `docs`: Documentation changes (e.g., README, guide files)
- `test`: Adding or modifying tests

#### Formatting Rules
- Description starts with a lowercase letter.
- Use imperative, present-tense mood (e.g., `add`, `update`, `fix`, `remove`, not `added`, `updates`, or `fixing`).
- No trailing period (`.`) at the end of the commit subject line.
- Keep the summary line concise (within 72 characters).
