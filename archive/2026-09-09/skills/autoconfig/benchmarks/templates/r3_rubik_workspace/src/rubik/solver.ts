import type { CubeState } from "./cubeState";

export function solveCube(state: CubeState): CubeState {
  return {
    ...state,
    scrambleHistory: []
  };
}
