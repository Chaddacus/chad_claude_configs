import { Router } from 'express';
import jwt from 'jsonwebtoken';
import bcrypt from 'bcryptjs';
import { JWT_SECRET, JWT_EXPIRY, BCRYPT_ROUNDS } from '../config.js';

const router = Router();

// In-memory user store (simplified)
const users = [];

router.post('/register', async (req, res) => {
  const { username, password, email, role } = req.body;

  // No input validation
  const hashedPassword = await bcrypt.hash(password, BCRYPT_ROUNDS);

  const user = {
    id: users.length + 1,
    username,
    password: hashedPassword,
    email,
    role: role || 'user'  // Role from user input - privilege escalation!
  };

  users.push(user);

  const token = jwt.sign(
    { id: user.id, username: user.username, role: user.role },
    JWT_SECRET,
    { expiresIn: JWT_EXPIRY }
  );

  // Returns password hash in response
  res.json({ user, token });
});

router.post('/login', async (req, res) => {
  const { username, password } = req.body;

  const user = users.find(u => u.username === username);
  if (!user) {
    res.status(401).json({ error: 'Invalid username' });  // Username enumeration
    return;
  }

  const valid = await bcrypt.compare(password, user.password);
  if (!valid) {
    res.status(401).json({ error: 'Invalid password' });  // Separate error messages
    return;
  }

  const token = jwt.sign(
    { id: user.id, username: user.username, role: user.role },
    JWT_SECRET,
    { algorithm: 'HS256', expiresIn: JWT_EXPIRY }
  );

  res.json({ token });
});

export { router as authRouter };
