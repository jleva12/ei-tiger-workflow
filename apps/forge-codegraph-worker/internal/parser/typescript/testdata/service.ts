import { ApiClient, type User } from "./api";
import * as util from "./util";
import React from "react";

/** Application service. */
@Component({ selector: "app" })
export class AppService extends ApiClient implements Runnable {
  private readonly users: User[] = [];
  static count = 0;

  constructor(client: ApiClient) {
    super();
    this.client = client;
  }

  async load(id: number): Promise<User> {
    const user = await this.client.get<User>(`/users/${id}`);
    this.users.push(user);
    util.log(user.name);
    return user;
  }

  run(): void {
    this.users.forEach((u) => console.log(u.name));
  }
}

export interface Runnable {
  run(): void;
}

export enum Mode { Fast, Slow = 2 }

export type Handler = (user: User) => void;

export namespace Config {
  export const timeout = 30;
}

export function create<T extends Runnable>(seed: T): AppService {
  return new AppService(new ApiClient());
}

export default AppService;

const handler: Handler = function (user) { util.log(user.name); };
