import * as lifecycle from "../src/components/forge/assistant/turn-lifecycle.ts"
import { AdkEventAccumulator } from "../node_modules/@assistant-ui/react-google-adk/dist/AdkEventAccumulator.js"
import { convertAdkMessage } from "../node_modules/@assistant-ui/react-google-adk/dist/convertAdkMessages.js"
import { createAdkStream } from "../node_modules/@assistant-ui/react-google-adk/dist/AdkClient.js"
import { convertExternalMessages } from "../node_modules/@assistant-ui/core/dist/runtime/utils/external-message-conversion.js"
import { testTurnLifecycle } from "../../../packages/forge-ui/tests/turn-lifecycle-cases.mjs"

testTurnLifecycle({
  ...lifecycle,
  AdkEventAccumulator,
  convertAdkMessage,
  createAdkStream,
  convertExternalMessages,
})
