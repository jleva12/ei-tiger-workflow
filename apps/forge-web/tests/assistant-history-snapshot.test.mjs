import * as history from "../src/components/forge/assistant/history-snapshot.ts"
import { convertAdkMessage } from "@assistant-ui/react-google-adk"
import { convertExternalMessages } from "../node_modules/@assistant-ui/core/dist/runtime/utils/external-message-conversion.js"
import { testHistorySnapshot } from "../../../packages/forge-ui/tests/history-snapshot-cases.mjs"

testHistorySnapshot({ ...history, convertAdkMessage, convertExternalMessages })
