const path = require("path");
import { helper } from "./helper.js";

export class Store {
  constructor(name) {
    this.name = name;
  }
  save(item) {
    return helper(item, this.name);
  }
}

export default function main() {
  return new Store(path.basename("x"));
}
