import { createContext } from "react";
import type { TicketSpec } from "./OrderTicket";

/** "sim", or a Schwab account's key ("live" for the first, "live-5678" for others). */
export type Account = string;

/** Whether an account is a Schwab one (anything but the simulated account). */
export const isSchwab = (a: Account) => a !== "sim";

/** A Schwab account the app runs, named as in Schwab. */
export interface SchwabAccount {
  key: string;
  name: string;
  last4: string;
}

/** Opens the order ticket from anywhere in the app. */
export const TicketContext = createContext<(spec: TicketSpec) => void>(() => {});

/** The account trading acts in: the one picked, or under All the first Schwab account. */
export const AccountContext = createContext<Account>("live");

/** "All" in the picker: every Schwab account together (never the simulated one). */
export const ALL = "all";

/** What Positions, Portfolio, Activity and Dashboard show: the account picked, or ALL. */
export const ViewContext = createContext<Account>("live");

/** The Schwab accounts in use, for names. */
export const AccountsContext = createContext<SchwabAccount[]>([]);
