import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import tseslint from 'typescript-eslint'
import { defineConfig, globalIgnores } from 'eslint/config'

export default defineConfig([
  globalIgnores(['dist', 'public/r']),
  {
    files: ['**/*.{ts,tsx}'],
    extends: [
      js.configs.recommended,
      tseslint.configs.recommended,
      reactHooks.configs.flat.recommended,
      reactRefresh.configs.vite,
    ],
    languageOptions: {
      globals: globals.browser,
    },
  },
  {
    // shadcn primitives export their cva variants and managers next to the components.
    files: ['src/components/ui/**/*.{ts,tsx}'],
    rules: {
      'react-refresh/only-export-components': [
        'error',
        {
          allowExportNames: [
            'buttonVariants',
            'badgeVariants',
            'tabsListVariants',
            'toggleVariants',
            'toast',
            'createToastManager',
            'useToastManager',
          ],
        },
      ],
    },
  },
  {
    // assistant-ui's elements, vendored from its registry: kept close to
    // upstream so updates merge, rather than reshaped for these rules.
    files: ['src/components/assistant-ui/**/*.{ts,tsx}'],
    rules: {
      'react-refresh/only-export-components': 'off',
      'react-hooks/refs': 'off',
      'no-empty': ['error', { allowEmptyCatch: true }],
    },
  },
])
