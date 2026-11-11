import { createContext } from "react";
import type { TicketSpec } from "./OrderTicket";

export type Account = "live" | "sim";

/** Opens the order ticket from anywhere in the app. */
export const TicketContext = createContext<(spec: TicketSpec) => void>(() => {});

/** The account the screens are showing. */
export const AccountContext = createContext<Account>("live");
