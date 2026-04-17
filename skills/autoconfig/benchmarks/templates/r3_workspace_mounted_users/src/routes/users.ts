import { Router } from 'express';

const router = Router();

interface User {
  id: number;
  name: string;
  email: string;
  age?: number;
}

const users: User[] = [];
let nextId = 1;

router.get('/', (_req, res) => {
  res.json(users);
});

router.post('/', (req, res) => {
  const user: User = {
    id: nextId++,
    name: req.body.name,
    email: req.body.email,
    age: req.body.age,
  };
  users.push(user);
  res.status(201).json(user);
});

router.get('/:id', (req, res) => {
  const user = users.find(u => u.id === parseInt(req.params.id));
  if (!user) {
    res.status(404).json({ error: 'User not found' });
    return;
  }
  res.json(user);
});

export default router;
