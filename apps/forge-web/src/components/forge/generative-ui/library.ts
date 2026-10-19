import {
  defaultGenerativeUILibrary,
  type GenerativeUILibrary,
} from "@assistant-ui/generative-ui/react"
import { RICH_ANSWER_COMPONENTS, type RichAnswerComponent } from "./catalog"
import { forgeComponent } from "./adapters"
import { Card } from "./components/card"
import { Row } from "./components/row"
import { Col } from "./components/col"
import { Tabs } from "./components/tabs"
import { Carousel } from "./components/carousel"
import { Box } from "./components/box"
import { Spacer } from "./components/spacer"
import { Divider } from "./components/divider"
import { Header } from "./components/header"
import { Text } from "./components/text"
import { Caption } from "./components/caption"
import { RichMarkdown } from "./components/markdown"
import { Badge } from "./components/badge"
import { Icon } from "./components/icon"
import { Alert } from "./components/alert"
import { Citation } from "./components/citation"
import { RichFact } from "./components/fact"
import { RichTable } from "./components/table"
import { RichChart } from "./components/chart"
import { RichSteps } from "./components/steps"
import { Timeline } from "./components/timeline"
import { ListView } from "./components/list-view"
import { ListViewItem } from "./components/list-view-item"
import { Image } from "./components/image"
import { RichDiagram } from "./components/diagram"
import { RichMap } from "./components/map"
import { RichChoices } from "./components/choices"
import { Button } from "./components/button"
import { Select } from "./components/select"
import { Input } from "./components/input"
import { DatePicker } from "./components/date-picker"
import { Checkbox } from "./components/checkbox"
import { Slider } from "./components/slider"
import { RadioGroup } from "./components/radio-group"
import { CheckboxGroup } from "./components/checkbox-group"
import { Form } from "./components/form"

/** A renderer for every catalog entry; missing or extra entries fail typechecking. */
const components = {
  Card,
  Row,
  Col,
  Tabs,
  Carousel,
  Box,
  Spacer,
  Divider,
  Header,
  Text,
  Caption,
  Markdown: forgeComponent("Markdown", RichMarkdown),
  Badge,
  Icon,
  Alert,
  Citation,
  Fact: forgeComponent("Fact", RichFact),
  Table: forgeComponent("Table", RichTable),
  Chart: forgeComponent("Chart", RichChart),
  Steps: forgeComponent("Steps", RichSteps),
  Timeline,
  ListView,
  ListViewItem,
  Image,
  Diagram: forgeComponent("Diagram", RichDiagram),
  Map: forgeComponent("Map", RichMap),
  Choices: forgeComponent("Choices", RichChoices),
  Button,
  Select,
  Input,
  DatePicker: {
    ...defaultGenerativeUILibrary.DatePicker,
    render: DatePicker,
  } as GenerativeUILibrary[string],
  Checkbox,
  Slider,
  RadioGroup,
  CheckboxGroup,
  Form,
} satisfies Record<RichAnswerComponent, GenerativeUILibrary[string]>

/** Every component the chat draws, in the same order as the agent's catalog. */
export const LIBRARY: GenerativeUILibrary = Object.fromEntries(
  RICH_ANSWER_COMPONENTS.map((name) => [name, components[name]])
)
