export type CubeState = {
  faces: string[];
  scrambleHistory: string[];
  isSolved: boolean;
};

export function createSolvedCube(): CubeState {
  return {
    faces: ["U", "R", "F", "D", "L", "B"],
    scrambleHistory: [],
    isSolved: true
  };
}

export function scrambleCube(state: CubeState, seed: number): CubeState {
  return {
    ...state,
    scrambleHistory: state.scrambleHistory.concat(`seed:${seed}`)
  };
}
