# VibeReading

Independent, local code-reading GUI. No editor integration or external AI service is required.

- Read source files without executing or modifying the inspected project.
- Show project -> file -> function structure and conservative static call relationships; classes and modules are clickable tags.
- Refresh after files are saved; preserve useful selection and human annotations.
- Support Python, R, Java, C, C++, Rust, JavaScript, TypeScript and Go through language adapters.
- Unresolved or ambiguous calls must stay explicit; syntax parsing is not proof of runtime behavior.
- Keep manual annotations separate from extracted comments and bind approval to a code version.
- Use small modules, documented APIs, commented scripts and reproducible tests.
- Do not overwrite user source or delete annotation history. Use project-local dependencies and state.
- Keep the UI in clear Chinese, with source identifiers unchanged.
- Record implementation limits in README.md and next work in NEXT_STEP.md.
