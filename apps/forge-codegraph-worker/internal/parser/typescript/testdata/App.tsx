import React, { useState } from "react";
import { Button } from "./button";
import * as Layout from "./layout";

export function App(props: { title: string }) {
  const [count, setCount] = useState(0);
  return (
    <div className="app">
      <Button onClick={() => setCount(count + 1)}>{props.title}</Button>
      <Layout.Header />
    </div>
  );
}
