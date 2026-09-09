import { Router } from 'express';
import { authenticate, authorize } from '../middleware/auth.js';

const router = Router();

router.get('/profile', authenticate, (req, res) => {
  res.json({ user: req.user });
});

router.get('/admin', authenticate, authorize('admin'), (req, res) => {
  res.json({ message: 'Admin panel', users: 'all users data here' });
});

// No auth middleware! Unprotected endpoint
router.get('/users', (req, res) => {
  res.json({ message: 'User list endpoint' });
});

export { router as protectedRouter };
