import { describe, expect, it } from "vitest";
import { createSolvedCube, scrambleCube } from "../src/rubik/cubeState";
import { solveCube } from "../src/rubik/solver";

describe("rubik state benchmark", () => {
  it("scramble produces a non-solved state and solve returns to solved", () => {
    const solved = createSolvedCube();
    const scrambled = scrambleCube(solved, 7);

    expect(scrambled.isSolved).toBe(false);
    expect(scrambled.scrambleHistory.length).toBeGreaterThan(0);

    const resolved = solveCube(scrambled);

    expect(resolved.isSolved).toBe(true);
    expect(resolved.scrambleHistory).toHaveLength(0);
  });
});
