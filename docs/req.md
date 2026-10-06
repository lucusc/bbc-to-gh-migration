Github
Source: BitBucket
- verify if the repo exists and is accessible
- 


LFS (Large File Storage)
Source: BitBucket
- use BB API to check if the repo has LFS enabled
- only warn if LFS is enabled and LFS fails at a step during the migration

After Migration checklist


- automate branch protection rule
- default branch set correctly.
- use shared workflow by copying a template that is in the shared workflow project
- create env variables and secrets configured correctly.


- [ ] All branches + tags present; default branch correct.
- [ ] LFS objects resolve. (if used)