import tseslint from 'typescript-eslint'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import jsxA11y from 'eslint-plugin-jsx-a11y'
import globals from 'globals'

// ESLint 10 flat config on the Fleet HQ baseline (CBN-170). Projects
// extend this rather than diverging from it — see Fleet HQ's
// docs/STACK-TENDENCIES.md §Frontend.
export default tseslint.config(
  { ignores: ['dist'] },
  {
    files: ['**/*.{ts,tsx}'],
    extends: [
      // Type-aware, not just syntax-aware (NAT-222). `recommendedTypeChecked`
      // is `recommended` plus the rules that need a type checker —
      // `no-floating-promises`, `no-misused-promises`, and the `no-unsafe-*`
      // family. The unawaited async call and the `any` leaking out of
      // `JSON.parse` are invisible without it.
      tseslint.configs.recommendedTypeChecked,
      // eslint-plugin-react-hooks v7 keeps its flat configs under `.flat`;
      // the bare `configs['recommended-latest']` is the eslintrc shape and
      // ESLint 10 rejects its string `plugins` array.
      reactHooks.configs.flat['recommended-latest'],
      reactRefresh.configs.vite,
      // Accessibility rules that are mechanically decidable — alt text, label
      // association, `aria-*` validity, positive tabindex. `recommended`, not
      // `strict`, matching every other plugin in this baseline. Its `eslint`
      // peer range still stops at ^9, so package.json carries an `overrides`
      // block to unblock `npm install` against the ESLint 10 baseline;
      // delete it once upstream widens.
      jsxA11y.flatConfigs.recommended,
    ],
    languageOptions: {
      ecmaVersion: 2020,
      globals: globals.browser,
      parserOptions: {
        // `projectService` lets typescript-eslint find each file's tsconfig
        // itself instead of being handed a `project` list. Its one requirement:
        // every file ESLint lints must be included by some tsconfig, or it
        // errors with "was not found by the project service". tsconfig.json is
        // a solution root whose references cover src/, tests/ and the config
        // files — if you add a TS file outside those include sets, add it to
        // one of them too.
        projectService: true,
        tsconfigRootDir: import.meta.dirname,
      },
    },
    rules: {
      // `caughtErrors: 'all'` is typescript-eslint v8's own default — pinned
      // here so an upstream default flip cannot silently stop flagging
      // `catch (err) {}`. A catch you genuinely want to ignore takes no
      // binding at all (`catch {}`), which this does not report.
      '@typescript-eslint/no-unused-vars': ['error', { caughtErrors: 'all' }],
    },
  },
)
