// Each feature module registers its tabs, menu items and toolbar buttons on import.
// Keep one import per line so parallel branches merge cleanly.
import "./maintenance";
import "./monitoring";
import "./replication";
import "./update-check";
import "./objects";
export {};
import "./schema";
import "./security";
import "./alerts";
import "./storage";
import "./datacompare";
import "./embedded";
import "./upgrade";
